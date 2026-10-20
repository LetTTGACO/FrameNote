"""Download retained video, muxing a separate retained audio track on demand."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from .service_pipeline import probe_media
from .service_process import run_process
from .task_store import atomic_json


def retained_file(directory: Path, name: object) -> Path | None:
    if not isinstance(name, str) or not name or Path(name).name != name or "\\" in name:
        return None
    path = directory / name
    if path.is_file() and path.resolve().is_relative_to(directory.resolve()) and path.stat().st_size > 0:
        return path
    return None


def retained_video(directory: Path, task: dict) -> Path | None:
    return retained_file(directory, task.get("assets", {}).get("video"))


def fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {"name": path.name, "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


async def prepare_video(directory: Path, task: dict, timeout: float) -> Path:
    video = retained_video(directory, task)
    if video is None:
        raise ValueError("Source video is not ready")
    media = await probe_media(video)
    if any(stream.get("codec_type") == "audio" for stream in media.get("streams", [])):
        return video
    audio = None
    acquisition = retained_file(directory, "acquisition.json")
    if acquisition:
        try:
            audio = retained_file(directory, json.loads(acquisition.read_text())["audio"])
        except (KeyError, ValueError, TypeError):
            pass
    audio = audio or retained_file(directory, task.get("assets", {}).get("audio"))
    if audio is None or audio.resolve() == video.resolve():
        return video
    identity = {"video": fingerprint(video), "audio": fingerprint(audio)}
    output, marker = directory / "video.download.mp4", directory / "video.download.json"
    cached = retained_file(directory, output.name)
    if cached and marker.is_file() and not marker.is_symlink():
        try:
            if json.loads(marker.read_text()) == {**identity, "output": fingerprint(cached)}:
                return cached
        except (ValueError, OSError):
            pass
    pending = directory / f"video.download.{uuid.uuid4().hex}.mp4"
    try:
        await run_process(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(video), "-i", str(audio),
                           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                           "-shortest", "-movflags", "+faststart", "-y", str(pending)], timeout=timeout)
        pending.replace(output)
        atomic_json(marker, {**identity, "output": fingerprint(output)})
        return output
    finally:
        pending.unlink(missing_ok=True)
