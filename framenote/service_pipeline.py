"""Prefer platform subtitles, falling back to isolated media acquisition and ASR."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time
from typing import Callable
from urllib.parse import urlsplit

from .galaxy_downloader import GalaxyDownloader
from .service_process import run_process
from .service_settings import Settings
from .asr import create_backend
from .asr.types import ASRBackend, ASRRequest, ASRProgress, ASRError
from .task_store import TaskStore, atomic_json, public_error
from .transcript import finite_number
from .materials import publish_asr, publish_subtitles, write_subtitle_attempts
from .subtitles import BilibiliInputError, SubtitleAttempt, SubtitleOutcome
from .acquisition import MediaDownloader, MediaSource, bilibili_page_url, normalize_video_input, youtube_video_url


def platform_url(value: str) -> str:
    url = normalize_video_input(value)
    if not url:
        raise ValueError("请提供B站、抖音或 YouTube 视频链接；本地视频请先上传。")
    parsed = urlsplit(url)
    source = MediaDownloader.detect_source(url)
    if source not in {MediaSource.BILIBILI, MediaSource.DOUYIN, MediaSource.YOUTUBE} or parsed.username or parsed.port not in {None, 80, 443}:
        raise ValueError("仅接受B站、抖音和 YouTube 平台链接。")
    if source == MediaSource.YOUTUBE:
        return youtube_video_url(url)
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

    def _acquisition_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        env["FRAMENOTE_PROCESS_TIMEOUT"] = str(self.settings.process_timeout)
        credentials = self.settings.bilibili_credentials
        if credentials is None:
            env.pop("FRAMENOTE_BILIBILI_CREDENTIALS", None)
        else:
            env["FRAMENOTE_BILIBILI_CREDENTIALS"] = str(credentials)
        return env

    async def acquire_subtitles(self, task_id: str, aborted: Callable[[], bool]) -> SubtitleOutcome:
        directory = self.store.directory(task_id)
        if aborted():
            raise asyncio.CancelledError
        try:
            await run_process([sys.executable, "-m", "framenote.service_acquire", "--root", str(self.store.root),
                               "--task-id", task_id, "--url", self.store.get(task_id)["video_url"],
                               "--mode", "subtitles"], timeout=self.settings.process_timeout,
                              log=directory / "acquisition.log", env=self._acquisition_environment())
            if aborted():
                raise asyncio.CancelledError
            payload = json.loads((directory / "subtitle-acquisition.json").read_text())
            outcome = SubtitleOutcome.from_payload(payload)
        except BilibiliInputError:
            raise
        except (TimeoutError, asyncio.TimeoutError):
            outcome = SubtitleOutcome(None, None, [SubtitleAttempt("worker", None, "error", "deadline_exceeded")],
                                      "deadline_exceeded")
        except (ValueError, KeyError, TypeError, AttributeError):
            outcome = SubtitleOutcome(None, None, [SubtitleAttempt("worker", None, "error", "invalid_response")],
                                      "invalid_response")
        except (OSError, RuntimeError):
            outcome = SubtitleOutcome(None, None, [SubtitleAttempt("worker", None, "error", "worker_failed")],
                                      "worker_failed")
        if aborted():
            raise asyncio.CancelledError
        return outcome

    async def acquire_upstream(self, task_id: str, url: str) -> tuple[Path, Path, dict]:
        directory = self.store.directory(task_id)
        await run_process([sys.executable, "-m", "framenote.service_acquire", "--root", str(self.store.root),
                           "--task-id", task_id, "--url", url, "--mode", "full"], timeout=self.settings.process_timeout,
                          log=directory / "acquisition.log", env=self._acquisition_environment())
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
        url = task.get("source_identity", {}).get("canonical_url") or task["video_url"]
        source = MediaDownloader.detect_source(url)
        if source == MediaSource.DOUYIN and self.settings.galaxy_enabled:
            if aborted():
                raise asyncio.CancelledError
            self.store.log(task_id, "抖音使用Galaxy API直接获取")
        else:
            try:
                return await self.acquire_upstream(task_id, url)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not self.settings.galaxy_enabled:
                    raise
                if aborted():
                    raise asyncio.CancelledError
                self.store.log(task_id, f"{source.value} 平台获取失败，使用Galaxy API：" + public_error(error))
        self.store.update(task_id, phase="galaxy_download")
        video, audio, info = await GalaxyDownloader(self.settings.galaxy_base_url).download(url, directory)
        atomic_json(directory / "acquisition.json", {"video": video.name, "audio": audio.name,
                                                   "metadata": info, "notes": []})
        return video, audio, info

    async def run(self, task_id: str, aborted: Callable[[], bool]) -> None:
        directory = self.store.directory(task_id)
        started = time.perf_counter()
        self.store.update(task_id, phase="acquisition", progress=5)
        task = self.store.get(task_id)
        is_bilibili = (not task.get("upload_name") and
                       MediaDownloader.detect_source(task.get("video_url", "")) == MediaSource.BILIBILI)
        identity = None
        if is_bilibili:
            self.store.update(task_id, material_schema_version=3)
            if not task.get("force_asr", False):
                outcome = await self.acquire_subtitles(task_id, aborted)
                if aborted():
                    raise asyncio.CancelledError
                write_subtitle_attempts(directory, outcome.attempts)
                identity = outcome.identity
                if identity is None and outcome.fallback_reason == "unsafe_target":
                    raise BilibiliInputError("invalid_video_url", "Unsafe Bilibili video URL")
                if identity is not None:
                    self.store.update(task_id, source_identity=asdict(identity))
                if outcome.choice is not None and identity is not None:
                    preparation_seconds = time.perf_counter() - started
                    payload = publish_subtitles(directory, identity, outcome.choice, cancelled=aborted)
                    atomic_json(directory / "metadata.json", {
                        "title": identity.title, "source": "bilibili", "source_url": task["video_url"],
                        "duration": identity.duration, "duration_source": "platform_part",
                        "source_identity": asdict(identity), "completeness": "unknown",
                    })
                    if aborted():
                        raise asyncio.CancelledError
                    timings = {"output": "transcript", "source_kind": payload["source_kind"],
                               "segment_count": len(outcome.choice.segments), "timestamp_kind": "subtitle_interval",
                               "preparation_seconds": round(preparation_seconds, 3),
                               "total_seconds": round(time.perf_counter() - started, 3)}
                    self.store.update(task_id, status="completed", phase="completed", progress=100,
                                      title=identity.title, duration=identity.duration, result=timings)
                    return
                self.store.update(task_id, fallback_reason=outcome.fallback_reason)
        if aborted():
            raise asyncio.CancelledError
        video, audio_source, info = await self.acquire(task_id, aborted)
        probe = await probe_media(video)
        if not any(s.get("codec_type") == "video" for s in probe.get("streams", [])):
            raise RuntimeError("输入没有视频轨道")
        duration = float(probe["format"].get("duration") or 0)
        if duration <= 0:
            raise RuntimeError("无法读取有效视频时长")
        metadata = {**info, "duration": duration, "duration_source": "media_probe", "streams": probe["streams"]}
        if identity is not None:
            metadata.update(platform_duration=identity.duration, source_identity=asdict(identity),
                            source_url=task["video_url"])
        atomic_json(directory / "metadata.json", metadata)
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
        publish_asr(directory, result, duration=audio_duration, cancelled=aborted)
        timings = {"output": "transcript", "segment_count": len(result.segments), "timestamp_kind": result.timestamp_kind,
                   "preparation_seconds": round(preparation_seconds, 3), "asr_seconds": round(asr_seconds, 3),
                   "total_seconds": round(time.perf_counter() - started, 3)}
        if "load_seconds" in result.metrics:
            timings["model_load_seconds"] = result.metrics["load_seconds"]
        self.store.update(task_id, status="completed", phase="completed", progress=100,
                          asr_run=asdict(result.provenance), result=timings)
