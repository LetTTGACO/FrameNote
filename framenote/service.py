"""Headless FrameNote ASGI entry point: uvicorn framenote.service:app --workers 1."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import fcntl
import json
import math
from pathlib import Path
from typing import Literal
import uuid

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator

from .frame_extractor import extract_frame
from .service_mcp import create_mcp
from .service_pipeline import Pipeline, platform_url
from .service_settings import Settings
from .task_store import TERMINAL, TaskStore, atomic_json, public_error
from .transcript import TranscriptSegment, segments_to_prompt, segments_to_srt

ARTIFACT_NAMES = {"metadata.json", "transcript.json", "transcript.md", "transcript.srt", "asr.raw.json", "frames.json"}
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".ts"}


class TranscribeRequest(BaseModel):
    video_url: str = ""
    upload_task_id: str = ""

    @model_validator(mode="after")
    def one_input(self):
        if bool(self.video_url) == bool(self.upload_task_id):
            raise ValueError("Supply exactly one of video_url or upload_task_id")
        if self.video_url:
            self.video_url = platform_url(self.video_url)
        return self


class FramesRequest(BaseModel):
    timestamps: list[float] = Field(min_length=1, max_length=24)


class Jobs:
    def __init__(self, store: TaskStore, pipeline: Pipeline):
        self.store, self.pipeline = store, pipeline
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.active: asyncio.Task | None = None
        self.active_id: str | None = None
        self.worker: asyncio.Task | None = None
        self.stopping = False

    async def start(self) -> None:
        self.queue = asyncio.Queue()
        self.stopping = False
        for task_id in self.store.recover():
            self.queue.put_nowait(task_id)
        self.worker = asyncio.create_task(self.work())

    async def work(self) -> None:
        while True:
            task_id = await self.queue.get()
            try:
                if self.store.get(task_id)["status"] != "queued":
                    continue
                self.active_id = task_id
                self.store.update(task_id, status="processing")
                self.active = asyncio.create_task(self.pipeline.run(task_id, lambda: self.store.get(task_id)["status"] == "cancelled" or self.stopping))
                try:
                    await self.active
                except asyncio.CancelledError:
                    if self.stopping:
                        self.store.update(task_id, status="failed", phase="interrupted", error={"code": "interrupted", "message": "服务停止，已保留材料。"})
                        raise
                    self.store.update(task_id, status="cancelled", phase="cancelled")
                except Exception as error:
                    phase = self.store.get(task_id)["phase"]
                    self.store.update(task_id, status="failed", error={"code": phase + "_failed", "message": public_error(error)})
            finally:
                self.active = None
                self.active_id = None
                self.queue.task_done()

    def enqueue(self, task_id: str) -> None:
        self.store.update(task_id, status="queued", phase="queued")
        self.queue.put_nowait(task_id)

    def cancel(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        if task["status"] in TERMINAL:
            return task
        task = self.store.update(task_id, status="cancelled", phase="cancelled")
        if self.active_id == task_id and self.active:
            self.active.cancel()
        return task

    async def stop(self) -> None:
        self.stopping = True
        if self.worker:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker


def create_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    store = TaskStore(config.workspace / "tasks")
    jobs = Jobs(store, pipeline or Pipeline(config, store))
    frame_locks: dict[str, asyncio.Lock] = {}
    remote_mcp = create_mcp()
    mcp_app = remote_mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with (config.workspace / ".service.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Workspace already in use; run exactly one service worker") from exc
            await jobs.start()
            try:
                async with remote_mcp.session_manager.run():
                    yield
            finally:
                await jobs.stop()

    app = FastAPI(title="FrameNote", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.jobs = store, jobs

    @app.exception_handler(KeyError)
    async def not_found(request: Request, error: KeyError):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "service": "FrameNote", "mode": "headless", "asr": "SenseVoiceSmall-onnx-int8",
                "asr_models_present": all((p / "model_quant.onnx").is_file() for p in (config.sensevoice_model, config.vad_model)),
                "galaxy_enabled": config.galaxy_enabled}

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...), title: str = Form("", max_length=300)):
        name = Path((file.filename or "video.mp4").replace("\\", "/")).name
        suffix = Path(name).suffix.lower()
        if suffix not in VIDEO_SUFFIXES:
            raise HTTPException(422, "Unsupported video extension")
        task = store.create(title=title or Path(name).stem, source="local", upload_name="source" + suffix)
        task_id = task["task_id"]
        partial = store.directory(task_id) / (task["upload_name"] + ".part")
        size = 0
        try:
            with partial.open("wb") as output:
                while chunk := await file.read(1024**2):
                    size += len(chunk)
                    if size > config.max_upload_bytes:
                        raise HTTPException(413, "Video exceeds upload limit")
                    output.write(chunk)
            if size == 0:
                raise HTTPException(422, "Video is empty")
            partial.replace(store.directory(task_id) / task["upload_name"])
            store.update(task_id, uploaded_bytes=size)
        except BaseException:
            store.update(task_id, status="failed", phase="upload_failed")
            raise
        finally:
            partial.unlink(missing_ok=True)
            await file.close()
        return {"upload_task_id": task_id, "bytes": size}

    @app.post("/api/transcribe")
    async def transcribe(body: TranscribeRequest):
        if body.upload_task_id:
            task = store.get(body.upload_task_id)
            if task["status"] != "uploaded":
                raise HTTPException(409, "Upload has already been submitted")
        else:
            task = store.create(video_url=body.video_url, title="视频", source="online")
        jobs.enqueue(task["task_id"])
        return {"task_id": task["task_id"]}

    @app.get("/api/tasks")
    async def list_tasks(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        tasks = store.all()
        return {"tasks": tasks[offset:offset + limit], "total": len(tasks)}

    @app.get("/api/task/{task_id}")
    async def task_status(task_id: str):
        return store.get(task_id)

    @app.post("/api/task/{task_id}/cancel")
    async def cancel(task_id: str):
        return jobs.cancel(task_id)

    @app.get("/api/task/{task_id}/transcript")
    async def transcript(task_id: str, output_format: Literal["json", "markdown", "srt"] = "json",
                         offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500),
                         start: float = Query(0, ge=0, allow_inf_nan=False), end: float | None = Query(None, ge=0, allow_inf_nan=False)):
        store.get(task_id)
        path = store.directory(task_id) / "transcript.json"
        if not path.is_file():
            raise HTTPException(409, "Transcript is not ready")
        if end is not None and end < start:
            raise HTTPException(422, "end must be >= start")
        payload = json.loads(path.read_text())
        selected = [s for s in payload["segments"] if s["end"] > start and (end is None or s["start"] < end)]
        page = selected[offset:offset + limit]
        result = {key: value for key, value in payload.items() if key != "segments"}
        result.update(total_segments=len(selected), offset=offset, limit=limit, segments=page)
        if output_format != "json":
            objects = [TranscriptSegment(**s) for s in page]
            result["content"] = segments_to_prompt(objects) if output_format == "markdown" else segments_to_srt(objects)
        return result

    @app.post("/api/task/{task_id}/frames")
    async def frames(task_id: str, body: FramesRequest):
        async with frame_locks.setdefault(task_id, asyncio.Lock()):
            task = store.get(task_id)
            video = task.get("assets", {}).get("video")
            if not video:
                raise HTTPException(409, "Source video is not ready")
            if any(not math.isfinite(t) or t < 0 or t >= task["duration"] for t in body.timestamps):
                raise HTTPException(422, "Frame timestamps must be within the video duration")
            directory = store.directory(task_id)
            index_path = directory / "frames.json"
            index = json.loads(index_path.read_text()) if index_path.is_file() else []
            generated = []
            for timestamp in body.timestamps:
                frame_id = uuid.uuid4().hex
                try:
                    item = await asyncio.to_thread(extract_frame, directory / video, directory / "frames" / f"{frame_id}.jpg", timestamp)
                except (ValueError, RuntimeError) as error:
                    raise HTTPException(422, public_error(error)) from error
                item.update(frame_id=frame_id, url=f"/api/task/{task_id}/frames/{frame_id}")
                index.append(item)
                generated.append(item)
                atomic_json(index_path, index)
            return {"frames": generated}

    @app.get("/api/task/{task_id}/frames/{frame_id}")
    async def frame(task_id: str, frame_id: str):
        store.get(task_id)
        if len(frame_id) != 32 or any(c not in "0123456789abcdef" for c in frame_id):
            raise HTTPException(404, "Frame not found")
        path = store.directory(task_id) / "frames" / f"{frame_id}.jpg"
        if not path.is_file():
            raise HTTPException(404, "Frame not found")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/task/{task_id}/artifacts")
    async def artifacts(task_id: str):
        store.get(task_id)
        directory = store.directory(task_id)
        return {"artifacts": [{"name": name, "bytes": (directory / name).stat().st_size,
                                "url": f"/api/task/{task_id}/artifacts/{name}"}
                               for name in sorted(ARTIFACT_NAMES) if (directory / name).is_file()]}

    @app.get("/api/task/{task_id}/artifacts/{name}")
    async def artifact(task_id: str, name: str):
        store.get(task_id)
        path = store.directory(task_id) / name
        if name not in ARTIFACT_NAMES or not path.is_file():
            raise HTTPException(404, "Artifact not found")
        return FileResponse(path, filename=name)

    app.mount("/", mcp_app)
    return app


app = create_app()
