"""Headless FrameNote ASGI entry point: uvicorn framenote.service:app --workers 1."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import fcntl
import json
import math
from pathlib import Path
from typing import Any, Literal, cast
import uuid

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, model_validator

from .acquisition import MediaDownloader, MediaSource
from .frame_extractor import extract_frame
from .service_mcp import create_mcp
from .service_pipeline import Pipeline, platform_url
from .service_settings import Settings
from .service_video import prepare_video, retained_video
from .jobs import Jobs
from .materials import is_material_name, material_names
from .task_store import TaskStore, atomic_json, public_error
from .transcript import MaterialError, TranscriptSegment, read_transcript, segments_to_prompt, segments_to_srt

VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".ts"}


class TranscribeRequest(BaseModel):
    video_url: str = ""
    upload_task_id: str = ""
    force_asr: bool = False

    @model_validator(mode="after")
    def one_input(self):
        if bool(self.video_url) == bool(self.upload_task_id):
            raise ValueError("Supply exactly one of video_url or upload_task_id")
        if self.video_url:
            self.video_url = platform_url(self.video_url)
        return self


class FramesRequest(BaseModel):
    timestamps: list[float] = Field(min_length=1, max_length=24)



def create_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    store = TaskStore(config.workspace / "tasks")
    runtime_pipeline = pipeline if pipeline is not None else Pipeline(config, store)
    backend = runtime_pipeline.backend
    jobs = Jobs(store, runtime_pipeline)
    frame_locks: dict[str, asyncio.Lock] = {}
    video_locks: dict[str, asyncio.Lock] = {}

    def public_task(task: dict) -> dict:
        directory = store.directory(task["task_id"])
        available = (directory.resolve().parent == store.root and retained_video(directory, task) is not None)
        return {**task, "downloads": {"video": f"/api/task/{task['task_id']}/video"} if available else {}}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with (config.workspace / ".service.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Workspace already in use; run exactly one service worker") from exc
            await asyncio.to_thread(backend.health)
            await jobs.start()
            try:
                async with remote_mcp.session_manager.run():
                    yield
            finally:
                await jobs.stop()

    app = FastAPI(title="FrameNote", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    remote_mcp = create_mcp(app=app)
    mcp_app = remote_mcp.streamable_http_app()
    app.state.store, app.state.jobs, app.state.backend = store, jobs, backend

    @app.exception_handler(KeyError)
    async def not_found(request: Request, error: KeyError):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(MaterialError)
    async def material_error(request: Request, error: MaterialError):
        return JSONResponse({"detail": {"code": error.code, "message": str(error)}}, status_code=409)

    @app.get("/api/health")
    async def health():
        status = backend.health()
        return {"status": "ok", "service": "FrameNote", "mode": "headless", "asr": status.backend,
                "asr_models_present": status.configured, "backend": asdict(status), "galaxy_enabled": config.galaxy_enabled}

    @app.get("/api/ready")
    async def ready():
        status = backend.health()
        return JSONResponse(asdict(status), status_code=200 if status.available else 503)

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
            values: dict[str, Any] = {"video_url": body.video_url, "title": "视频", "source": "online",
                                      "force_asr": body.force_asr}
            if MediaDownloader.detect_source(body.video_url) == MediaSource.BILIBILI:
                values["material_schema_version"] = 3
            task = store.create(**values)
        jobs.enqueue(task["task_id"])
        return {"task_id": task["task_id"]}

    @app.get("/api/tasks")
    async def list_tasks(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        tasks = store.all()
        return {"tasks": [public_task(task) for task in tasks[offset:offset + limit]], "total": len(tasks)}

    @app.get("/api/task/{task_id}")
    async def task_status(task_id: str):
        return public_task(store.get(task_id))

    @app.get("/api/task/{task_id}/video")
    async def video(task_id: str, request: Request):
        async with video_locks.setdefault(task_id, asyncio.Lock()):
            task = store.get(task_id)
            directory = store.directory(task_id)
            if directory.resolve().parent != store.root or retained_video(directory, task) is None:
                raise HTTPException(409, "Source video is not ready")
            try:
                path = await prepare_video(directory, task, config.process_timeout)
            except (TimeoutError, asyncio.TimeoutError) as error:
                raise HTTPException(504, "Video preparation timed out; retry the download") from error
            except (ValueError, RuntimeError) as error:
                raise HTTPException(422, public_error(error)) from error
            suffix = ".mp4" if path.suffix.lower() == ".m4s" else path.suffix.lower()
            response = FileResponse(path, filename="video" + suffix, stat_result=path.stat())
            if request.headers.get("if-none-match") == response.headers.get("etag"):
                return Response(status_code=304, headers={"ETag": response.headers["etag"]})
            return response

    @app.post("/api/task/{task_id}/cancel")
    async def cancel(task_id: str):
        return jobs.cancel(task_id)

    @app.get("/api/task/{task_id}/transcript")
    async def transcript(task_id: str, output_format: Literal["json", "markdown", "srt"] = "json",
                         offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500),
                         start: float = Query(0, ge=0, allow_inf_nan=False), end: float | None = Query(None, ge=0, allow_inf_nan=False)):
        store.get(task_id)
        directory = store.directory(task_id)
        path = directory / "transcript.json"
        if directory.resolve().parent != store.root or "transcript.json" not in material_names(directory):
            raise HTTPException(409, "Transcript is not ready")
        if end is not None and end < start:
            raise HTTPException(422, "end must be >= start")
        payload = read_transcript(path)
        selected = [s for s in cast(list[dict[str, Any]], payload["segments"]) if s["end"] > start and (end is None or s["start"] < end)]
        page = selected[offset:offset + limit]
        result: dict[str, Any] = {key: value for key, value in payload.items() if key != "segments"}
        result.update(total_segments=len(selected), offset=offset, limit=limit, segments=page,
                      text="\n".join(segment["text"] for segment in page))
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
        task = store.get(task_id)
        directory = store.directory(task_id)
        names = material_names(directory) if directory.resolve().parent == store.root else []
        return {"schema_version": task.get("material_schema_version", 2), "task_id": task_id, "downloads": public_task(task)["downloads"],
                "artifacts": [{"name": name, "bytes": (directory / name).stat().st_size,
                                "url": f"/api/task/{task_id}/artifacts/{name}"}
                               for name in names]}

    @app.get("/api/task/{task_id}/artifacts/{name}")
    async def artifact(task_id: str, name: str):
        store.get(task_id)
        directory = store.directory(task_id)
        path = directory / name
        if not is_material_name(name) or directory.resolve().parent != store.root or name not in material_names(directory):
            raise HTTPException(404, "Artifact not found")
        return FileResponse(path, filename=name)

    app.mount("/", mcp_app)
    return app


app = create_app()
