#!/usr/bin/env python3
"""Remote headless service client. Standard library only; streamed uploads."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ARTIFACTS = {"metadata.json", "transcript.json", "transcript.md", "transcript.srt", "asr.raw.json", "frames.json"}


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("Service URL must be an http(s) endpoint")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, data: dict | None = None) -> dict:
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=120) as response:
            return json.load(response)

    def upload(self, path: Path) -> dict:
        # Never interpolate a local filename into a multipart header.
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", path.suffix.lower()):
            raise ValueError("Invalid video extension")
        boundary = uuid.uuid4().hex
        prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n{path.stem[:300]}\r\n"
                  f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                  f"filename=\"video{path.suffix.lower()}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        suffix = f"\r\n--{boundary}--\r\n".encode()
        url = urllib.parse.urlsplit(self.base)
        assert url.hostname is not None
        kind = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = kind(url.hostname, url.port, timeout=300)
        try:
            connection.putrequest("POST", url.path.rstrip("/") + "/api/upload")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(len(prefix) + path.stat().st_size + len(suffix)))
            connection.endheaders()
            connection.send(prefix)
            with path.open("rb") as stream:
                while chunk := stream.read(1024**2):
                    connection.send(chunk)
            connection.send(suffix)
            response = connection.getresponse()
            payload = json.loads(response.read())
            if response.status >= 400:
                raise RuntimeError(f"Upload HTTP {response.status}: {payload}")
            return payload
        finally:
            connection.close()

    def download(self, route: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".part")
        try:
            with self.opener.open(self.base + route, timeout=120) as response, temporary.open("wb") as output:
                while chunk := response.read(1024**2):
                    output.write(chunk)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def fetch(self, task_id: str, output: Path) -> dict:
        output.mkdir(parents=True, exist_ok=True)
        materials = output / "materials"
        for directory in (output, materials):
            marker = directory / "bundle.json"
            if marker.exists():
                if json.loads(marker.read_text())["task_id"] != task_id:
                    raise ValueError("材料目录属于其他任务，请使用另一个 --out-dir。")
            elif any((directory / name).exists() for name in ARTIFACTS):
                raise ValueError("目录已有未标记任务身份的材料，请使用新的 --out-dir。")
        if (output / "bundle.json").exists():
            raise ValueError("目录仍使用旧版布局，请先将原始材料及截图整理到 materials/。")
        materials.mkdir(parents=True, exist_ok=True)
        bundle_path = materials / "bundle.json"
        task = self.request("GET", f"/api/task/{task_id}")
        bundle = {"service_url": self.base, "task_id": task_id, "task": task, "directory": str(output.resolve()),
                  "materials_directory": str(materials.resolve()),
                  "download_complete": False}
        # Claim ownership before downloading so interrupted attempts remain identifiable.
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2))
        manifest = self.request("GET", f"/api/task/{task_id}/artifacts")
        for item in manifest["artifacts"]:
            name = item["name"]
            if name in ARTIFACTS:
                self.download(f"/api/task/{task_id}/artifacts/{name}", materials / name)
        if any(item["name"] == "frames.json" for item in manifest["artifacts"]):
            frames = json.loads((materials / "frames.json").read_text())
            for frame in frames:
                frame_id = frame["frame_id"]
                if not re.fullmatch(r"[0-9a-f]{32}", frame_id):
                    raise ValueError("Invalid frame id in server manifest")
                frame["relative_path"] = f"materials/frames/{frame_id}.jpg"
                self.download(f"/api/task/{task_id}/frames/{frame_id}", output / frame["relative_path"])
            (materials / "frames.json").write_text(json.dumps(frames, ensure_ascii=False, indent=2))
        bundle["download_complete"] = True
        bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2))
        return bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-url", default=os.getenv("FRAMENOTE_SERVICE_URL", "http://192.168.31.56:18121"))
    commands = parser.add_subparsers(dest="command", required=True)
    transcribe = commands.add_parser("transcribe")
    transcribe.add_argument("source", help="B站/抖音链接或Mac本地视频")
    transcribe.add_argument("--wait", type=float, default=1800)
    transcribe.add_argument("--out-dir", type=Path, required=True, help="笔记根目录；原始材料保存到其 materials/ 子目录")
    frames = commands.add_parser("frames")
    frames.add_argument("task_id")
    frames.add_argument("--timestamps", type=float, nargs="+", required=True)
    frames.add_argument("--out-dir", type=Path, required=True, help="同一任务的笔记根目录")
    fetch = commands.add_parser("fetch")
    fetch.add_argument("task_id")
    fetch.add_argument("--out-dir", type=Path, required=True, help="笔记根目录；原始材料保存到其 materials/ 子目录")
    for name in ("status", "cancel"):
        commands.add_parser(name).add_argument("task_id")
    args = parser.parse_args()
    client = Client(args.service_url)
    if args.command == "transcribe":
        source = Path(args.source).expanduser()
        try:
            local = source.is_file() if not args.source.startswith(("http://", "https://")) else False
        except OSError:
            local = False
        payload = {"upload_task_id": client.upload(source)["upload_task_id"]} if local else {"video_url": args.source}
        task_id = client.request("POST", "/api/transcribe", payload)["task_id"]
        print(json.dumps({"task_id": task_id}), file=sys.stderr, flush=True)
        deadline = time.monotonic() + max(0, args.wait)
        previous = None
        while True:
            task = client.request("GET", f"/api/task/{task_id}")
            message = (task["phase"], task["progress"])
            if message != previous:
                print(f"{task_id}: {message[0]} {message[1]}%", file=sys.stderr, flush=True)
                previous = message
            if task["status"] in {"completed", "failed", "cancelled"}:
                bundle = client.fetch(task_id, args.out_dir)
                print(json.dumps(bundle, ensure_ascii=False, indent=2))
                if task["status"] != "completed":
                    raise RuntimeError(task.get("error") or task["status"])
                return
            if time.monotonic() >= deadline:
                print(json.dumps(task, ensure_ascii=False, indent=2))
                raise TimeoutError(f"等待超时，服务继续处理；用status/fetch回读任务 {task_id}")
            time.sleep(3)
    elif args.command == "frames":
        client.request("POST", f"/api/task/{args.task_id}/frames", {"timestamps": args.timestamps})
        print(json.dumps(client.fetch(args.task_id, args.out_dir), ensure_ascii=False, indent=2))
    elif args.command == "fetch":
        print(json.dumps(client.fetch(args.task_id, args.out_dir), ensure_ascii=False, indent=2))
    else:
        method = "POST" if args.command == "cancel" else "GET"
        route = f"/api/task/{args.task_id}" + ("/cancel" if method == "POST" else "")
        print(json.dumps(client.request(method, route), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
