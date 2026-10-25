"""Acquisition subprocess: terminate the whole process group on cancellation."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

from .task_store import TaskStore, atomic_json
from .bilibili import BilibiliClient, _page, _site_url
from .credentials import load_credentials
from .platform_http import BilibiliHTTP, BilibiliHTTPError
from .service_settings import Settings
from .service_video import retained_file
from .subtitles import BilibiliIdentity, BilibiliInputError, fetch_bilibili_subtitles
from .acquisition import MediaDownloader, MediaSource


def resolve_media_source(root: Path, task_id: str, url: str) -> tuple[str, BilibiliIdentity | None, dict | None]:
    """Resolve identity on the subprocess main thread before any legacy downloader."""
    settings = Settings.from_env()
    credentials, _state = load_credentials(settings.bilibili_credentials, forbidden_root=root)
    http = BilibiliHTTP(credentials, deadline=time.monotonic() + settings.process_timeout)
    directory = root / task_id
    marker = directory / "source-resolution.json"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        target = _site_url(url)
        page = _page(target)
        target = _site_url(http.resolve_short_url(target))
        target = urlsplit(target)._replace(query="p=" + str(page or _page(target) or 1), fragment="").geturl()
        atomic_json(marker, {"url": target})
        expected = None
        for name in ("task.json", "metadata.json", "transcript.json"):
            path = retained_file(directory, name)
            if path:
                payload = json.loads(path.read_text())
                expected = payload.get("source_identity") or payload.get("provenance", {}).get("identity")
                if expected:
                    break
        identity: BilibiliIdentity | None
        try:
            identity = BilibiliClient(http).resolve(target)
        except BilibiliHTTPError:
            # Metadata outages do not disable safe legacy media acquisition.
            identity = BilibiliIdentity(**expected) if expected else None
        if expected and identity and any(expected.get(key) != getattr(identity, key) for key in ("bvid", "aid", "page", "cid")):
            atomic_json(marker, {"error": {"code": "media_identity_mismatch"}})
            raise RuntimeError("media_identity_mismatch")
        if identity:
            target = identity.canonical_url
            atomic_json(marker, {"url": target, "identity": asdict(identity)})
        cookie = None if credentials is None else {
            ("sessdata" if key == "SESSDATA" else key): value for key, value in credentials.cookies.items()
        }
        return target, identity, cookie
    except BilibiliHTTPError as error:
        if error.code != "unsafe_target":
            raise
        invalid = BilibiliInputError("invalid_video_url", "Unsafe Bilibili video URL")
        atomic_json(marker, {"error": {"code": invalid.code, "message": str(invalid)}})
        raise invalid from None
    except BilibiliInputError as error:
        atomic_json(marker, {"error": {"code": error.code, "message": str(error)}})
        raise


async def acquire(root: Path, task_id: str, url: str, *, mode: str = "full") -> None:
    processor = MediaDownloader(root)
    identity, cookie = None, None
    is_bilibili = processor.detect_source(url) == MediaSource.BILIBILI
    if is_bilibili:
        # This synchronous call remains on the isolated worker main thread.
        url, identity, cookie = resolve_media_source(root, task_id, url)
    notes: list[str] = []
    directory = root / task_id
    try:
        if identity:
            processor._bili_view_cache[identity.bvid] = {
                "bvid": identity.bvid, "aid": identity.aid,
                "pages": [{"page": identity.page, "cid": identity.cid, "part": identity.part, "duration": identity.duration}],
            }
            info = {"title": identity.title, "source": "bilibili"}
        else:
            info = await processor.get_video_info(url, cookie=cookie, notes=notes)

        async def track(kind: str) -> Path:
            if identity:
                suffix = "m4s" if kind == "video" else "m4a"
                target = directory / ("preview." + suffix if kind == "video" else "audio-original." + suffix)
                return await asyncio.to_thread(processor._download_bilibili_media_via_api,
                                               url, target, cookie, kind, None, notes)
            if kind == "video":
                return await processor.download_preview_video(url, task_id, cookie=cookie, notes=notes)
            return await processor.download_audio(url, task_id, cookie=cookie, notes=notes)

        asset = await track("audio" if mode == "audio" else "video")
        # Keep helper processes in this worker's process group for cancellation.
        media = json.loads(subprocess.run([
            "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(asset),
        ], capture_output=True, text=True, check=True, timeout=30).stdout)
        kind = "audio" if mode == "audio" else "video"
        if not any(stream.get("codec_type") == kind for stream in media.get("streams", [])):
            raise RuntimeError("Platform download has no requested track")
        has_audio = any(stream.get("codec_type") == "audio" for stream in media.get("streams", []))
        metadata: dict = {key: info.get(key, "") for key in ("title", "description", "owner", "source", "thumbnail")}
        metadata.update(acquisition_provider="upstream", source_url=url,
                        source_has_audio=True if has_audio else processor.source_has_audio,
                        identity_evidence="cid" if identity else "url")
        if identity:
            metadata["source_identity"] = asdict(identity)
        if mode != "full":
            atomic_json(directory / f"acquisition.{mode}.json", {"asset": asset.name, "metadata": metadata})
            return
        audio = asset if has_audio else await track("audio")
        if audio != asset:
            metadata["source_has_audio"] = True
        atomic_json(directory / "acquisition.json", {"video": asset.name, "audio": audio.name,
                                                     "metadata": metadata, "notes": notes})
    except Exception:
        if is_bilibili:
            raise RuntimeError("Bilibili media acquisition failed") from None
        raise


def acquire_subtitles(root: Path, task_id: str, url: str) -> None:
    directory = TaskStore(root).directory(task_id)
    settings = Settings.from_env()
    credentials, _state = load_credentials(settings.bilibili_credentials, forbidden_root=root)
    client = BilibiliClient(BilibiliHTTP(credentials, deadline=time.monotonic() + settings.process_timeout))
    try:
        payload = fetch_bilibili_subtitles(url, directory, client=client).to_payload()
    except BilibiliInputError as error:
        payload = {'error': {'code': error.code, 'message': str(error)}}
    atomic_json(directory / 'subtitle-acquisition.json', payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--mode", choices=("full", "subtitles", "video", "audio"), default="full")
    args = parser.parse_args()
    TaskStore(args.root).directory(args.task_id)
    with contextlib.redirect_stdout(sys.stderr):
        if args.mode == "subtitles":
            acquire_subtitles(args.root, args.task_id, args.url)
        else:
            asyncio.run(acquire(args.root, args.task_id, args.url, mode=args.mode))


if __name__ == "__main__":
    main()
