"""Acquire and retain media, normalize audio, then run the isolated ASR worker."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import shutil
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, cast
from urllib.parse import urlsplit

from .galaxy_downloader import GalaxyDownloader
from .service_process import run_process
from .service_settings import Settings
from .asr import create_backend
from .asr.types import ASRBackend, ASRRequest, ASRProgress, ASRError, result_to_payload
from .task_store import TaskStore, atomic_json, public_error
from .transcript import TranscriptSegment, finite_number, segments_to_prompt, segments_to_srt
from .acquisition import MediaDownloader, MediaSource, bilibili_page_url, normalize_video_input


def platform_url(value: str) -> str:
    url = normalize_video_input(value)
    if not url:
        raise ValueError("请提供B站或抖音视频链接；本地视频请先上传。")
    parsed = urlsplit(url)
    source = MediaDownloader.detect_source(url)
    if source not in {MediaSource.BILIBILI, MediaSource.DOUYIN} or parsed.username or parsed.port not in {None, 80, 443}:
        raise ValueError("仅接受B站和抖音平台链接。")
    if source == MediaSource.BILIBILI and parsed.hostname != "b23.tv" and "p=" not in parsed.query:
        url = bilibili_page_url(url, 1)
    return url


async def probe_media(path: Path) -> dict:
    raw = await run_process(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], timeout=30)
    return json.loads(raw)


class Pipeline:
    def __init__(self, settings: Settings, store: TaskStore, backend: ASRBackend | None = None):
        self.settings, self.store = settings, store
        self.backend = backend if backend is not None else create_backend(settings)

    async def acquire_upstream(self, task_id: str, url: str) -> tuple[Path, Path, dict]:
        directory = self.store.directory(task_id)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        await run_process([sys.executable, "-m", "framenote.service_acquire", "--root", str(self.store.root),
                           "--task-id", task_id, "--url", url], timeout=self.settings.process_timeout,
                          log=directory / "acquisition.log", env=env)
        result = json.loads((directory / "acquisition.json").read_text())
        for note in result["notes"]:
            self.store.log(task_id, public_error(RuntimeError(note)))
        return directory / result["video"], directory / result["audio"], result["metadata"]

    async def acquire(self, task_id: str, aborted: Callable[[], bool]) -> tuple[Path, Path, dict]:
        task = self.store.get(task_id)
        directory = self.store.directory(task_id)
        if task.get("upload_name"):
            path = directory / task["upload_name"]
            return path, path, {"title": task["title"], "source": "local", "acquisition_provider": "upload"}
        url = task["video_url"]
        source = MediaDownloader.detect_source(url)
        try:
            return await self.acquire_upstream(task_id, url)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if source != MediaSource.DOUYIN or not self.settings.galaxy_enabled:
                raise
            self.store.log(task_id, "原版抖音获取失败，使用Galaxy API：" + public_error(error))
            self.store.update(task_id, phase="galaxy_download")
            video, info = await GalaxyDownloader(self.settings.galaxy_base_url).download(url, directory)
            return video, video, info

    async def run(self, task_id: str, aborted: Callable[[], bool]) -> None:
        directory = self.store.directory(task_id)
        started = time.perf_counter()
        self.store.update(task_id, phase="acquisition", progress=5)
        video, audio_source, info = await self.acquire(task_id, aborted)
        probe = await probe_media(video)
        if not any(s.get("codec_type") == "video" for s in probe.get("streams", [])):
            raise RuntimeError("输入没有视频轨道")
        duration = float(probe["format"].get("duration") or 0)
        if duration <= 0:
            raise RuntimeError("无法读取有效视频时长")
        atomic_json(directory / "metadata.json", {**info, "duration": duration, "streams": probe["streams"]})
        self.store.update(task_id, title=info.get("title") or "视频", duration=duration,
                          acquisition_provider=info.get("acquisition_provider"),
                          assets={"video": video.name}, phase="audio", progress=20)
        audio = directory / "audio.flac"
        pending = directory / "audio.pending.flac"
        await run_process(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(audio_source),
                           "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "flac", "-frame_size", "4096",
                           "-y", str(pending)], timeout=self.settings.process_timeout, log=directory / "audio.log")
        pending.replace(audio)
        audio_probe = await probe_media(audio)
        audio_duration = float(audio_probe["format"]["duration"])
        if abs(audio_duration - duration) > max(3, duration * .01):
            raise RuntimeError("下载音轨与视频时长不一致，已保留材料供检查")
        preparation_seconds = time.perf_counter() - started
        self.store.update(task_id, assets={"video": video.name, "audio": audio.name}, phase="asr", progress=30)
        if not finite_number(audio_duration) or audio_duration <= 0 or not audio.resolve().is_relative_to(directory.resolve()):
            raise ASRError("asr_invalid_response", "Invalid normalized audio input")
        if aborted():
            raise asyncio.CancelledError
        self.store.update(task_id, asr_run=asdict(self.backend.describe()))

        def progress(value: ASRProgress) -> None:
            completed, total = value.completed, value.total
            if type(completed) is not int or type(total) is not int or total <= 0 or not 0 <= completed <= total:
                return
            self.store.update(task_id, progress=30 + int(65 * completed / total),
                              asr_segments={"completed": completed, "total": total})

        asr_started = time.perf_counter()
        result = await self.backend.transcribe(ASRRequest(audio, audio_duration, directory),
                                               on_progress=progress, cancelled=aborted)
        asr_seconds = time.perf_counter() - asr_started
        if aborted():
            raise asyncio.CancelledError
        payload = result_to_payload(result, duration=audio_duration)
        raw = result.raw_path.resolve()
        if not raw.is_relative_to(directory.resolve()) or not raw.is_file():
            raise ASRError("asr_invalid_response", "ASR raw artifact must belong to this task")
        if aborted():
            raise asyncio.CancelledError
        raw_path = directory / "asr.raw.json"
        if raw != raw_path.resolve():
            pending_raw = directory / "asr.raw.pending.json"
            try:
                shutil.copyfile(raw, pending_raw)
                pending_raw.replace(raw_path)
            finally:
                pending_raw.unlink(missing_ok=True)
        segments = [TranscriptSegment(**item) for item in cast(list[dict[str, Any]], payload["segments"])]
        atomic_json(directory / "transcript.json", payload)
        explanation = ("时间范围来自VAD语音片段，非词级时间戳。" if result.timestamp_kind == "VAD_interval"
                       else "时间范围来自转录后端的分段区间，非词级时间戳。")
        (directory / "transcript.md").write_text(
            "# 未人工校对的转录原稿\n\n" + explanation + "\n\n" + segments_to_prompt(segments) + "\n", encoding="utf-8")
        (directory / "transcript.srt").write_text(segments_to_srt(segments), encoding="utf-8", newline="")
        timings = {"output": "transcript", "segment_count": len(segments), "timestamp_kind": result.timestamp_kind,
                   "preparation_seconds": round(preparation_seconds, 3), "asr_seconds": round(asr_seconds, 3),
                   "total_seconds": round(time.perf_counter() - started, 3)}
        if "load_seconds" in result.metrics:
            timings["model_load_seconds"] = result.metrics["load_seconds"]
        self.store.update(task_id, status="completed", phase="completed", progress=100,
                          asr_run=asdict(result.provenance), result=timings)
