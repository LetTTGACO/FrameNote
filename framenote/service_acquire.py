"""Acquisition subprocess: terminate the whole process group on cancellation."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import subprocess
import sys

from .task_store import atomic_json
from .acquisition import MediaDownloader, MediaSource, resolve_bilibili_url


async def acquire(root: Path, task_id: str, url: str) -> None:
    processor = MediaDownloader(root)
    if processor.detect_source(url) == MediaSource.BILIBILI:
        url = resolve_bilibili_url(url)
    notes: list[str] = []
    info = await processor.get_video_info(url, notes=notes)
    video = await processor.download_preview_video(url, task_id, notes=notes)
    # Keep helper processes in this worker's process group for parent cancellation.
    media = json.loads(subprocess.run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video),
    ], capture_output=True, text=True, check=True, timeout=30).stdout)
    if not any(s["codec_type"] == "video" for s in media["streams"]):
        raise RuntimeError("平台下载结果没有视频轨道")
    audio = video if any(s["codec_type"] == "audio" for s in media["streams"]) else await processor.download_audio(url, task_id, notes=notes)
    metadata = {key: info.get(key, "") for key in ("title", "description", "owner", "source", "thumbnail")}
    metadata.update(acquisition_provider="upstream", source_url=url)
    atomic_json(root / task_id / "acquisition.json", {"video": video.name, "audio": audio.name, "metadata": metadata, "notes": notes})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    with contextlib.redirect_stdout(sys.stderr):
        asyncio.run(acquire(args.root, args.task_id, args.url))


if __name__ == "__main__":
    main()
