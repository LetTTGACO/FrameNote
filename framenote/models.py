"""Explicit, integrity-checked model preparation; standard library only."""
from __future__ import annotations

import argparse
import hashlib
from importlib import resources
import json
from pathlib import Path, PurePosixPath
import re
import sys
import time
from typing import Any
import urllib.request
from urllib.parse import urlsplit
import uuid


def _assets(manifest: Path | None) -> list[dict[str, Any]]:
    raw = manifest.read_text(encoding="utf-8") if manifest else resources.files("framenote.asr").joinpath("models.lock.json").read_text(encoding="utf-8")
    lock = json.loads(raw)
    if not isinstance(lock, dict) or type(lock.get("schema_version")) is not int or lock["schema_version"] != 1:
        raise ValueError("Unsupported model manifest version")
    files = lock.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Model manifest requires files")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Invalid model asset")
        target = item.get("target")
        if not isinstance(target, str):
            raise ValueError("Model target must be a relative path")
        parts = PurePosixPath(target)
        if (parts.is_absolute() or len(parts.parts) != 2 or parts.parts[0] not in {"sensevoice", "vad"}
                or ".." in parts.parts or "\\" in target or parts.as_posix() != target or target in seen):
            raise ValueError("Unsafe or duplicate model target: " + target)
        seen.add(target)
        if type(item.get("bytes")) is not int or item["bytes"] <= 0:
            raise ValueError("Invalid asset size: " + target)
        if not isinstance(item.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", item["sha256"]):
            raise ValueError("Invalid asset SHA-256: " + target)
        if not isinstance(item.get("revision"), str) or not re.fullmatch("[0-9a-f]{40}", item["revision"]):
            raise ValueError("Asset requires an immutable revision: " + target)
        for key in ("url", "license_url"):
            value = item.get(key)
            if not isinstance(value, str):
                raise ValueError("Invalid asset URL: " + target)
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Invalid asset URL: " + target)
        if not isinstance(item.get("repo"), str) or not item["repo"]:
            raise ValueError("Asset source missing: " + target)
    return files


def required_assets(*, manifest: Path | None = None) -> dict[str, tuple[str, ...]]:
    groups: dict[str, list[str]] = {}
    for item in _assets(manifest):
        group, name = item["target"].split("/")
        groups.setdefault(group, []).append(name)
    return {group: tuple(names) for group, names in groups.items()}


def _target(root: Path, asset: dict[str, Any]) -> Path:
    target = root / asset["target"]
    if not target.resolve().is_relative_to(root):
        raise ValueError("Model path escapes output directory: " + asset["target"])
    return target


def _valid(path: Path, asset: dict[str, Any]) -> bool:
    if not path.is_file() or path.stat().st_size != asset["bytes"]:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            digest.update(chunk)
    return digest.hexdigest() == asset["sha256"]


def verify_models(output: Path, *, manifest: Path | None = None) -> None:
    files = _assets(manifest)
    root = output.expanduser().resolve()
    paths = [_target(root, asset) for asset in files]
    failures = [asset["target"] for asset, path in zip(files, paths) if not _valid(path, asset)]
    if failures:
        raise ValueError("Missing or corrupt model assets: " + ", ".join(failures))


def prepare_models(output: Path, *, manifest: Path | None = None) -> None:
    files = _assets(manifest)
    root = output.expanduser().resolve()
    paths = [_target(root, asset) for asset in files]
    for asset, target in zip(files, paths):
        if _valid(target, asset):
            continue
        _target(root, asset)
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".part")
        print("Downloading " + asset["target"], file=sys.stderr, flush=True)
        try:
            deadline = time.monotonic() + 120
            request = urllib.request.Request(asset["url"], headers={"User-Agent": "FrameNote/0.1 model-preparation"})
            with urllib.request.urlopen(request, timeout=120) as response, pending.open("wb") as stream:
                size = 0
                while chunk := response.read(1024**2):
                    size += len(chunk)
                    if size > asset["bytes"] or time.monotonic() > deadline:
                        raise ValueError("Download exceeded size or time limit: " + asset["target"])
                    stream.write(chunk)
            if not _valid(pending, asset):
                raise ValueError("Download integrity check failed: " + asset["target"])
            _target(root, asset)
            pending.replace(target)
        finally:
            pending.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "verify"])
    parser.add_argument("--out", type=Path, default=Path("models"))
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare_models(args.out, manifest=args.manifest)
        verify_models(args.out, manifest=args.manifest)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print("Verified all model assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
