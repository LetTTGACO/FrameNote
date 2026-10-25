"""Prefer platform subtitles, falling back to isolated media acquisition and ASR."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
import re
from pathlib import Path
import sys
import time
from typing import Callable, Literal
from urllib.parse import urlsplit

from .galaxy_downloader import GalaxyDownloader
from .service_process import probe_media, run_process
from .service_video import fingerprint, media_manifest, prepare_video, retained_file, retained_video
from .bilibili import _page, _site_url
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

    def _material(self, directory: Path, name: str) -> dict:
        path = retained_file(directory, name)
        if path:
            try:
                payload = json.loads(path.read_text())
                if isinstance(payload, dict):
                    return payload
            except (OSError, ValueError):
                pass
        return {}

    def _source_identity(self, task: dict, directory: Path) -> dict:
        return (task.get("source_identity") or self._material(directory, "metadata.json").get("source_identity")
                or self._material(directory, "transcript.json").get("provenance", {}).get("identity") or {})

    def _acquisition_input_error(self, directory: Path) -> None:
        error = self._material(directory, "source-resolution.json").get("error")
        if error and error.get("code") in {"invalid_video_url", "invalid_video_part"}:
            raise BilibiliInputError(error["code"], error.get("message", "Invalid Bilibili input"))
        if error and error.get("code") in {"media_identity_mismatch", "media_duration_mismatch"}:
            raise RuntimeError(error["code"])

    def _galaxy_url(self, directory: Path, url: str) -> str:
        if MediaDownloader.detect_source(url) != MediaSource.BILIBILI:
            return url
        self._acquisition_input_error(directory)
        if urlsplit(url).hostname == "b23.tv":
            url = self._material(directory, "source-resolution.json").get("url", "")
            if not url:
                raise RuntimeError("Bilibili short-link resolution failed")
        target = _site_url(url)
        parsed = urlsplit(target)
        if not re.fullmatch(r"/video/(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]*)/?", parsed.path):
            raise BilibiliInputError("invalid_video_url", "Invalid Bilibili video URL")
        return parsed._replace(query="p=" + str(_page(target) or 1), fragment="").geturl()

    async def acquire_asset(self, task_id: str, url: str, *, kind: Literal["video", "audio"]) -> tuple[Path, dict]:
        if kind not in {"video", "audio"}:
            raise ValueError("Unsupported media asset kind")
        directory = self.store.directory(task_id)
        task = self.store.get(task_id)
        if task.get("upload_name"):
            path = retained_file(directory, task["upload_name"])
            if path is None:
                raise RuntimeError("media_unavailable")
            return path, {"source": "local", "acquisition_provider": "upload"}
        identity = self._source_identity(task, directory)
        url = identity.get("canonical_url") or url
        source = MediaDownloader.detect_source(url)
        result_path = directory / f"acquisition.{kind}.json"
        result_path.unlink(missing_ok=True)
        (directory / "source-resolution.json").unlink(missing_ok=True)
        if source != MediaSource.DOUYIN or not self.settings.galaxy_enabled:
            try:
                await run_process([sys.executable, "-m", "framenote.service_acquire", "--root", str(self.store.root),
                                   "--task-id", task_id, "--url", url, "--mode", kind],
                                  timeout=self.settings.process_timeout, log=directory / "acquisition.log",
                                  env=self._acquisition_environment())
                result = json.loads(result_path.read_text())
                asset = retained_file(directory, result["asset"])
                if asset is None:
                    raise RuntimeError("media_unavailable")
                return asset, result["metadata"]
            except asyncio.CancelledError:
                raise
            except BilibiliInputError:
                raise
            except Exception:
                self._acquisition_input_error(directory)
                if not self.settings.galaxy_enabled:
                    raise RuntimeError("media_acquisition_failed") from None
        url = self._galaxy_url(directory, url)
        downloader = GalaxyDownloader(self.settings.galaxy_base_url)
        method = downloader.download_video if kind == "video" else downloader.download_audio
        try:
            asset, info = await method(url, directory)
        except Exception:
            raise RuntimeError("media_acquisition_failed") from None
        atomic_json(result_path, {"asset": asset.name, "metadata": info})
        return asset, info

    async def _validate_asset(self, directory: Path, asset: Path, *, kind: str, identity: dict,
                              duration: object, metadata: dict) -> tuple[dict, float]:
        retained = retained_file(directory, asset.name)
        if retained is None or retained.resolve() != asset.resolve():
            raise RuntimeError("media_unavailable")
        actual_identity = metadata.get("source_identity") or {}
        for key in ("bvid", "aid", "page", "cid"):
            if key in identity and key in actual_identity and identity[key] != actual_identity[key]:
                raise RuntimeError("media_identity_mismatch")
        before = fingerprint(asset)
        try:
            probe = await probe_media(asset)
            actual_duration = float(probe.get("format", {}).get("duration") or 0)
        except Exception:
            raise RuntimeError("media_invalid") from None
        if not finite_number(actual_duration) or actual_duration <= 0 or before != fingerprint(asset):
            raise RuntimeError("media_invalid")
        if not any(stream.get("codec_type") == kind for stream in probe.get("streams", [])):
            raise RuntimeError("audio_unavailable" if kind == "audio" else "media_invalid")
        for reference in (duration, identity.get("duration"), actual_identity.get("duration"), metadata.get("duration")):
            if reference is not None:
                if (not isinstance(reference, (int, float)) or not finite_number(reference) or reference <= 0
                        or abs(actual_duration - reference) > max(3., reference * .01)):
                    raise RuntimeError("media_duration_mismatch")
        return probe, actual_duration

    async def prepare_media(self, task_id: str, *, require_audio: bool, cancelled: Callable[[], bool]) -> Path:
        directory = self.store.directory(task_id)
        task = self.store.get(task_id)
        metadata = self._material(directory, "metadata.json")
        manifest = media_manifest(directory)
        identity = self._source_identity(task, directory)
        duration = identity.get("duration") or metadata.get("duration") or task.get("duration")
        url = identity.get("canonical_url") or task.get("video_url", "")

        def check_cancelled() -> None:
            if cancelled():
                raise asyncio.CancelledError

        def publish() -> None:
            manifest["fingerprints"] = {kind: fingerprint(path) for kind in ("video", "audio", "download")
                                        if (path := retained_file(directory, manifest.get(kind))) is not None}
            atomic_json(directory / "media.json", manifest)

        try:
            check_cancelled()
            acquisition = self._material(directory, "acquisition.json")
            video_acquisition = self._material(directory, "acquisition.video.json")
            video = retained_video(directory, task)
            info = manifest or acquisition.get("metadata", {})
            if video is None:
                video = retained_file(directory, video_acquisition.get("asset"))
                info = video_acquisition.get("metadata", {})
            if video is None:
                video = retained_file(directory, acquisition.get("video"))
                info = acquisition.get("metadata", {})
            if video is None:
                video, info = await self.acquire_asset(task_id, url, kind="video")
            probe, video_duration = await self._validate_asset(directory, video, kind="video", identity=identity,
                                                               duration=duration, metadata=info)
            embedded_audio = any(stream.get("codec_type") == "audio" for stream in probe.get("streams", []))
            has_audio = True if embedded_audio else info.get("source_has_audio")
            if type(has_audio) is not bool:
                has_audio = None
            if manifest.get("fingerprints", {}).get("video") != fingerprint(video):
                manifest.pop("download", None)
            manifest.update(video=video.name, source_identity=identity or info.get("source_identity", {}),
                            source_has_audio=has_audio, duration=video_duration, duration_verified=True,
                            identity_evidence=info.get("identity_evidence", "unknown"))
            # Retain verified video even if a later audio acquisition is interrupted.
            publish()
            check_cancelled()
            if not require_audio or embedded_audio:
                if embedded_audio:
                    manifest["download"] = video.name
                    publish()
                return video
            audio_acquisition = self._material(directory, "acquisition.audio.json")
            audio = None
            audio_info: dict = {}
            candidates = (
                (manifest.get("audio"), manifest),
                (acquisition.get("audio"), acquisition.get("metadata", {})),
                (audio_acquisition.get("asset"), audio_acquisition.get("metadata", {})),
                (task.get("assets", {}).get("audio"), metadata),
                ("audio.flac", metadata),
            )
            for name, evidence in candidates:
                candidate = retained_file(directory, name)
                if candidate is not None and candidate.resolve() != video.resolve():
                    audio, audio_info = candidate, evidence
                    break
            if audio is None:
                if has_audio is False:
                    manifest["download"] = video.name
                    publish()
                    return video
                try:
                    audio, audio_info = await self.acquire_asset(task_id, url, kind="audio")
                except BilibiliInputError:
                    raise
                except Exception:
                    raise RuntimeError("audio_unavailable") from None
            await self._validate_asset(directory, audio, kind="audio", identity=identity,
                                       duration=video_duration, metadata=audio_info)
            if manifest.get("fingerprints", {}).get("audio") != fingerprint(audio):
                manifest.pop("download", None)
            manifest.update(audio=audio.name, source_has_audio=True)
            publish()
            check_cancelled()
            output = await prepare_video(directory, task, self.settings.process_timeout, source_has_audio=True)
            await self._validate_asset(directory, output, kind="video", identity=identity,
                                       duration=video_duration, metadata={})
            check_cancelled()
            manifest["download"] = output.name
            publish()
            return output
        finally:
            for pattern in ("*.part", "*.pending.*"):
                for path in directory.glob(pattern):
                    if path.is_file() and not path.is_symlink():
                        path.unlink(missing_ok=True)

    async def acquire_upstream(self, task_id: str, url: str) -> tuple[Path, Path, dict]:
        directory = self.store.directory(task_id)
        (directory / "source-resolution.json").unlink(missing_ok=True)
        try:
            await run_process([sys.executable, "-m", "framenote.service_acquire", "--root", str(self.store.root),
                               "--task-id", task_id, "--url", url, "--mode", "full"], timeout=self.settings.process_timeout,
                              log=directory / "acquisition.log", env=self._acquisition_environment())
        except Exception:
            self._acquisition_input_error(directory)
            raise
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
        url = self._source_identity(task, directory).get("canonical_url") or task["video_url"]
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
            except BilibiliInputError:
                raise
            except Exception as error:
                self._acquisition_input_error(directory)
                if not self.settings.galaxy_enabled:
                    raise
                if aborted():
                    raise asyncio.CancelledError
                self.store.log(task_id, f"{source.value} 平台获取失败，使用Galaxy API：" + public_error(error))
        url = self._galaxy_url(directory, url)
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
