"""Optional Galaxy resolver with retained media and separate audio support."""
from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from .acquisition import MediaDownloader, MediaSource
from .service_process import run_process


class GalaxyDownloader:
    HTTP_CHUNK_BYTES = 8 << 20

    def __init__(self, base_url: str):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("Invalid Galaxy API base URL")
        self.base_url = base_url.rstrip("/")

    def _media_url(self, data: dict[str, Any], kind: str) -> str:
        name = "Video" if kind == "video" else "Audio"
        value = data.get(f"download{name}Url") or data.get(f"originDownload{name}Url")
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"Galaxy response has no {kind} download URL")
        url = urljoin(self.base_url + "/", value)
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
            raise RuntimeError("Unsupported Galaxy media URL")
        return url

    async def _fetch_media(self, url: str, partial: Path) -> None:
        # Bounded ranges avoid throttling of full YouTube audio transfers. Only
        # append complete, verified chunks so a retry cannot duplicate bytes.
        chunk = partial.with_name(partial.name + ".chunk.part")
        headers = partial.with_name(partial.name + ".headers.part")
        offset = 0
        total: int | None = None
        try:
            while total is None or offset < total:
                end = offset + self.HTTP_CHUNK_BYTES - 1
                await run_process([
                    "curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=http,https",
                    "--connect-timeout", "15", "--max-time", "1800", "--retry", "2", "--retry-all-errors",
                    "--range", f"{offset}-{end}", "--header", "Accept-Encoding: identity",
                    "--dump-header", str(headers), "--output", str(chunk), url,
                ], timeout=1900, log=partial.parent / "download.log")
                status = 0
                fields: dict[str, str] = {}
                # Redirects, proxy CONNECT and retries can each add a block.
                for line in headers.read_text(encoding="latin-1").splitlines():
                    if line.startswith("HTTP/"):
                        status = int(line.split()[1])
                        fields = {}
                    elif ":" in line:
                        name, value = line.split(":", 1)
                        fields[name.lower()] = value.strip()
                if status == 200:
                    # A server may ignore Range and return the entire object.
                    chunk.replace(partial)
                    return
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", fields.get("content-range", ""))
                if status != 206 or match is None:
                    raise RuntimeError("Galaxy media returned an invalid byte range")
                first, last, size = map(int, match.groups())
                if (first != offset or last < first or last > end or last >= size
                        or (total is not None and size != total) or chunk.stat().st_size != last - first + 1):
                    raise RuntimeError("Galaxy media returned an inconsistent byte range")
                with partial.open("wb" if offset == 0 else "ab") as output, chunk.open("rb") as source:
                    shutil.copyfileobj(source, output)
                total = size
                offset = last + 1
                chunk.unlink()
                headers.unlink()
        finally:
            chunk.unlink(missing_ok=True)
            headers.unlink(missing_ok=True)

    async def _download_media(self, url: str, target: Path, kind: str) -> tuple[Path, dict]:
        partial = target.with_name(target.name + ".part")
        remux = target.with_name(target.name + ".remux.part")
        try:
            await asyncio.wait_for(self._fetch_media(url, partial), timeout=1900)
            if not partial.is_file() or partial.stat().st_size == 0:
                raise RuntimeError("Galaxy download is empty")
            with partial.open("rb") as stream:
                is_hls = stream.read(1024).decode("utf-8-sig", errors="replace").lstrip().startswith("#EXTM3U")
            candidate = partial
            if is_hls:
                # Use the remote playlist so relative segment URLs resolve correctly.
                # Restrict nested playlist/key URLs to network protocols.
                streams = ["-map", "0:v:0", "-map", "0:a:0?"] if kind == "video" else ["-map", "0:a:0"]
                await run_process([
                    "ffmpeg", "-nostdin", "-v", "error", "-xerror",
                    "-protocol_whitelist", "http,https,httpproxy,tcp,tls,crypto", "-rw_timeout", "15000000",
                    "-i", url, *streams, "-c", "copy", "-f", "mp4" if kind == "video" else "matroska",
                    "-y", str(remux),
                ], timeout=1900, log=target.parent / "download.log")
                candidate = remux
            raw = await run_process(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                                     "-of", "json", str(candidate)], timeout=30)
            media = json.loads(raw)
            duration = float(media.get("format", {}).get("duration") or 0)
            if not math.isfinite(duration) or duration <= 0:
                raise RuntimeError("Galaxy media has no valid duration")
            if not any(stream.get("codec_type") == kind for stream in media.get("streams", [])):
                raise RuntimeError(f"Galaxy media has no {kind} track")
            candidate.replace(target)
            return target, media
        finally:
            partial.unlink(missing_ok=True)
            remux.unlink(missing_ok=True)

    async def download(self, url: str, directory: Path) -> tuple[Path, Path, dict]:
        source = MediaDownloader.detect_source(url)
        if source not in {MediaSource.BILIBILI, MediaSource.DOUYIN, MediaSource.YOUTUBE}:
            raise ValueError("Unsupported Galaxy source platform")
        args = ["curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=http,https",
                "--connect-timeout", "15", "--max-time", "120", "--retry", "2", "--retry-all-errors",
                "--retry-delay", "2", "--retry-max-time", "180"]
        # A file lets curl reset partial responses on retry, unlike a stdout pipe.
        response_path = directory / "galaxy-response.private.json"
        try:
            await run_process([*args, "--output", str(response_path),
                               self.base_url + "/api/parse?" + urlencode({"url": url})], timeout=220,
                              log=directory / "galaxy.log")
            payload = json.loads(response_path.read_text())
        finally:
            response_path.unlink(missing_ok=True)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or payload.get("success") is not True:
            raise RuntimeError("Galaxy API did not return a successful result")
        reported_platform = data.get("platform")
        if reported_platform == "bili":
            reported_platform = "bilibili"
        if reported_platform not in {None, source.value}:
            raise RuntimeError("Galaxy returned a different platform")
        video, media = await self._download_media(self._media_url(data, "video"), directory / "source.mp4", "video")
        # Inspect tracks rather than relying on the API's videoAudioMode hint.
        audio = video
        if not any(stream.get("codec_type") == "audio" for stream in media.get("streams", [])):
            audio, _ = await self._download_media(self._media_url(data, "audio"), directory / "source-audio.media", "audio")
        # Keep source identity; discard expiring URLs and unrelated API fields.
        return video, audio, {"title": str(data.get("title") or "视频"),
                              "description": str(data.get("description") or data.get("desc") or ""),
                              "owner": str(data.get("author") or ""), "source": source.value,
                              "acquisition_provider": "galaxy_api", "source_url": url}
