"""Optional external resolver. curl matches the verified Galaxy API client path."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from .service_process import run_process


class GalaxyDownloader:
    def __init__(self, base_url: str):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("Invalid Galaxy API base URL")
        self.base_url = base_url.rstrip("/")

    async def download(self, url: str, directory: Path) -> tuple[Path, dict]:
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
        data = payload.get("data") or {}
        if payload.get("success") is not True or not isinstance(data, dict):
            raise RuntimeError("Galaxy API did not return a successful result")
        if data.get("platform") not in {None, "douyin"}:
            raise RuntimeError("Galaxy returned a different platform")
        download_url = data.get("downloadVideoUrl") or data.get("originDownloadVideoUrl")
        if not isinstance(download_url, str) or not download_url:
            raise RuntimeError("Galaxy response has no video download URL")
        download_url = urljoin(self.base_url + "/", download_url)
        if urlsplit(download_url).scheme not in {"https", "http"}:
            raise RuntimeError("Unsupported Galaxy media URL scheme")
        target = directory / "source.mp4"
        partial = directory / "source.mp4.part"
        try:
            await run_process([
                "curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=http,https",
                "--connect-timeout", "15", "--max-time", "1800", "--retry", "2",
                "--output", str(partial), download_url,
            ], timeout=1900, log=directory / "download.log")
            if not partial.is_file() or partial.stat().st_size == 0:
                raise RuntimeError("Galaxy download is empty")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        # Keep source identity; discard expiring URLs and unrelated API fields.
        return target, {"title": str(data.get("title") or "抖音视频"),
                        "description": str(data.get("description") or data.get("desc") or ""),
                        "owner": str(data.get("author") or ""), "source": "douyin",
                        "acquisition_provider": "galaxy_api", "source_url": url}
