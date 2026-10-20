#!/usr/bin/env python3
"""FrameNote HTTP client. Python standard library only; streamed file transfers."""
from __future__ import annotations

import argparse
from email.message import Message
import hashlib
import http.client
import json
import math
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
CONFIG_KEYS = {"service_url", "notes_dir", "download_video"}
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".ts", ".m4s"}


def check_version(payload: dict) -> None:
    version = payload.get("schema_version", 1)
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("Unsupported material schema version")


def user_config_path() -> Path:
    return Path.home() / ".config/framenote/client.json"


def project_config_path(cwd: Path) -> Path | None:
    for directory in (cwd, *cwd.parents):
        candidate = directory / ".framenote.json"
        if candidate.exists():
            return candidate
        if (directory / ".git").exists():
            break
    return None


def read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"配置文件不是有效 JSON：{path}: {error.msg}") from error
    if not isinstance(value, dict):
        raise ValueError(f"配置必须是 JSON 对象：{path}")
    unknown = value.keys() - CONFIG_KEYS
    if unknown:
        raise ValueError(f"未知配置项：{', '.join(sorted(unknown))}（{path}）")
    for key in ("service_url", "notes_dir"):
        if key in value and (not isinstance(value[key], str) or not value[key].strip()):
            raise ValueError(f"{key} 必须是非空字符串（{path}）")
    if "download_video" in value and type(value["download_video"]) is not bool:
        raise ValueError(f"download_video 必须是 true 或 false（{path}）")
    if "notes_dir" in value:
        directory = Path(value["notes_dir"]).expanduser()
        value["notes_dir"] = str((directory if directory.is_absolute() else path.parent / directory).resolve())
    return value


def boolean(value: str) -> bool:
    if value.strip().lower() in {"true", "yes", "1", "on"}:
        return True
    if value.strip().lower() in {"false", "no", "0", "off"}:
        return False
    raise ValueError("FRAMENOTE_DOWNLOAD_VIDEO 必须是 true/false、yes/no、1/0 或 on/off")


def service_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.query or parsed.fragment):
        raise ValueError("Service URL must be an http(s) base URL without credentials, query or fragment")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid service port")
    return value


def default_notes_dir(cwd: Path | None = None) -> Path:
    directory = (cwd or Path.cwd()).resolve()
    for root in (directory, *directory.parents):
        if (root / "pyproject.toml").is_file() and (root / "framenote/service.py").is_file():
            return root / "workspace/notes"
        if (root / ".git").exists():
            return root / "framenote-notes"
    return directory / "framenote-notes"


def resolve_config(*, cwd: Path | None = None, user_path: Path | None = None,
                   environ: dict | None = None, **overrides) -> dict:
    directory = (cwd or Path.cwd()).resolve()
    user = user_path if user_path is not None else user_config_path()
    project = project_config_path(directory)
    result = {"service_url": "http://127.0.0.1:18121", "notes_dir": str(default_notes_dir(directory)),
              "download_video": False}
    sources = {key: "default" for key in result}
    for path in (user, project):
        if path is not None:
            values = read_config(path)
            result.update(values)
            sources.update({key: str(path) for key in values})
    environment = os.environ if environ is None else environ
    for key, variable in (("service_url", "FRAMENOTE_SERVICE_URL"), ("notes_dir", "FRAMENOTE_NOTES_DIR"),
                          ("download_video", "FRAMENOTE_DOWNLOAD_VIDEO")):
        if variable in environment:
            result[key] = boolean(environment[variable]) if key == "download_video" else environment[variable]
            sources[key] = variable
    for key, value in overrides.items():
        if key in CONFIG_KEYS and value is not None:
            result[key] = value
            sources[key] = "command line"
    result["service_url"] = service_url(result["service_url"])
    if not isinstance(result["notes_dir"], (str, Path)) or not str(result["notes_dir"]).strip():
        raise ValueError("notes_dir 必须是非空路径")
    notes = Path(result["notes_dir"]).expanduser()
    result["notes_dir"] = str((notes if notes.is_absolute() else directory / notes).resolve())
    result.update(project_config=str(project) if project else None,
                  user_config=str(user) if user.exists() else None, sources=sources)
    return result


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            value.update(chunk)
    return value.hexdigest()


class Client:
    def __init__(self, base: str):
        self.base = service_url(base)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, data: dict | None = None) -> dict:
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=120) as response:
            return json.load(response)

    def upload(self, path: Path) -> dict:
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

    def save_response(self, response, path: Path) -> dict:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
        size, checksum = 0, hashlib.sha256()
        try:
            with temporary.open("wb") as output:
                while chunk := response.read(1024**2):
                    output.write(chunk)
                    size += len(chunk)
                    checksum.update(chunk)
            length = response.headers.get("Content-Length")
            if length is not None and size != int(length):
                raise OSError("Download interrupted: Content-Length mismatch")
            temporary.replace(path)
            return {"bytes": size, "sha256": checksum.hexdigest()}
        finally:
            temporary.unlink(missing_ok=True)

    def download(self, route: str, path: Path) -> dict:
        with self.opener.open(self.base + route, timeout=120) as response:
            return self.save_response(response, path)

    def retrieve_video(self, task: dict, output: Path, previous: dict | None = None) -> dict:
        route = f"/api/task/{task['task_id']}/video"
        if task.get("downloads", {}).get("video") != route:
            raise ValueError("服务未提供此任务的视频下载入口；请确认视频已就绪并升级服务。")
        headers = {}
        if previous and previous.get("url") == route:
            relative = previous.get("relative_path", "")
            path = output / relative
            if (Path(relative).parts[:1] == ("materials",) and len(Path(relative).parts) == 2
                    and Path(relative).suffix.lower() in VIDEO_SUFFIXES and path.is_file()
                    and path.stat().st_size == previous.get("bytes") and digest(path) == previous.get("sha256")
                    and previous.get("etag")):
                headers["If-None-Match"] = previous["etag"]
        request = urllib.request.Request(self.base + route, headers=headers)
        try:
            response = self.opener.open(request, timeout=120)
        except urllib.error.HTTPError as error:
            if error.code == 304 and headers:
                error.close()
                return previous
            raise
        with response:
            disposition = Message()
            disposition["Content-Disposition"] = response.headers.get("Content-Disposition", "")
            filename = disposition.get_filename()
            if (not filename or Path(filename).name != filename or "\\" in filename
                    or Path(filename).suffix.lower() not in VIDEO_SUFFIXES):
                raise ValueError("Invalid video filename returned by service")
            path = output / "materials" / ("video" + Path(filename).suffix.lower())
            info = self.save_response(response, path)
            return {**info, "relative_path": path.relative_to(output).as_posix(), "url": route,
                    "etag": response.headers.get("ETag")}

    def fetch(self, task_id: str, output: Path, *, download_video: bool = False) -> dict:
        materials = output / "materials"
        previous = {}
        for directory in (output, materials):
            marker = directory / "bundle.json"
            if marker.exists():
                value = json.loads(marker.read_text(encoding="utf-8"))
                check_version(value)
                if value["task_id"] != task_id:
                    raise ValueError("材料目录属于其他任务，请使用另一个 --out-dir。")
                if value.get("service_url", self.base).rstrip("/") != self.base:
                    raise ValueError("材料目录属于其他服务，请使用另一个 --out-dir。")
                previous = value
            elif any((directory / name).exists() for name in ARTIFACTS):
                raise ValueError("目录已有未标记任务身份的材料，请使用新的 --out-dir。")
        if (output / "bundle.json").exists():
            raise ValueError("目录仍使用旧版布局，请先将原始材料及截图整理到 materials/。")
        task = self.request("GET", f"/api/task/{task_id}")
        if task.get("task_id") != task_id:
            raise ValueError("Service returned a different task")
        manifest = self.request("GET", f"/api/task/{task_id}/artifacts")
        check_version(manifest)
        if manifest.get("task_id", task_id) != task_id:
            raise ValueError("Artifact manifest belongs to a different task")
        materials.mkdir(parents=True, exist_ok=True)
        bundle_path = materials / "bundle.json"
        bundle = {"schema_version": 2, "service_url": self.base, "task_id": task_id, "task": task,
                  "directory": str(output.resolve()), "materials_directory": str(materials.resolve()),
                  "download_video": download_video, "download_complete": False}
        if "video" in previous:
            bundle["video"] = previous["video"]
        write_json(bundle_path, bundle)
        for item in manifest["artifacts"]:
            name = item["name"]
            if name in ARTIFACTS:
                self.download(f"/api/task/{task_id}/artifacts/{name}", materials / name)
        if any(item["name"] == "frames.json" for item in manifest["artifacts"]):
            frames = json.loads((materials / "frames.json").read_text(encoding="utf-8"))
            for frame in frames:
                frame_id = frame["frame_id"]
                if not re.fullmatch(r"[0-9a-f]{32}", frame_id):
                    raise ValueError("Invalid frame id in server manifest")
                frame["relative_path"] = f"materials/frames/{frame_id}.jpg"
                self.download(f"/api/task/{task_id}/frames/{frame_id}", output / frame["relative_path"])
            (materials / "frames.json").write_text(json.dumps(frames, ensure_ascii=False, indent=2), encoding="utf-8")
        if download_video:
            bundle["video"] = self.retrieve_video(task, output, previous.get("video"))
        bundle["download_complete"] = True
        write_json(bundle_path, bundle)
        return bundle

    def wait(self, task_id: str, timeout: float) -> dict:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("等待时间必须是有限的非负数")
        deadline, previous = time.monotonic() + timeout, None
        while True:
            task = self.request("GET", f"/api/task/{task_id}")
            message = (task.get("phase"), task.get("progress"))
            if message != previous:
                print(f"{task_id}: {message[0]} {message[1]}%", file=sys.stderr, flush=True)
                previous = message
            if task["status"] in {"completed", "failed", "cancelled"} or time.monotonic() >= deadline:
                return task
            time.sleep(min(3, max(0, deadline - time.monotonic())))


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--service-url", default=argparse.SUPPRESS)
    common.add_argument("--notes-dir", default=argparse.SUPPRESS)
    common.add_argument("--download-video", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS,
                        help="取回服务端保留的视频；--no-download-video 覆盖自动取回配置")
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    commands = parser.add_subparsers(dest="command", required=True)
    def command(name, **kwargs):
        return commands.add_parser(name, parents=[common], **kwargs)
    transcribe = command("transcribe")
    transcribe.add_argument("source", help="B站/抖音/YouTube 链接或本地视频路径")
    transcribe.add_argument("--wait", type=float, default=1800)
    transcribe.add_argument("--out-dir", type=Path, required=True, help="笔记根目录；材料归入 materials/")
    frames = command("frames")
    frames.add_argument("task_id")
    frames.add_argument("--timestamps", type=float, nargs="+", required=True)
    frames.add_argument("--out-dir", type=Path, required=True)
    fetch = command("fetch")
    fetch.add_argument("task_id")
    fetch.add_argument("--out-dir", type=Path, required=True)
    for name in ("status", "cancel", "artifacts"):
        command(name).add_argument("task_id")
    wait = command("wait")
    wait.add_argument("task_id")
    wait.add_argument("--timeout", type=float, default=45)
    tasks = command("tasks")
    tasks.add_argument("--offset", type=int, default=0)
    tasks.add_argument("--limit", type=int, default=20)
    transcript = command("transcript")
    transcript.add_argument("task_id")
    transcript.add_argument("--start", type=float, default=0)
    transcript.add_argument("--end", type=float)
    transcript.add_argument("--offset", type=int, default=0)
    transcript.add_argument("--limit", type=int, default=200)
    transcript.add_argument("--output-format", choices=("json", "markdown", "srt"), default="json")
    command("notes-dir", help="显示默认笔记父目录")
    command("config", help="显示生效配置及其来源")
    command("health", help="检查服务连接与后端状态")
    args = parser.parse_args()
    config = resolve_config(**{key: getattr(args, key, None) for key in CONFIG_KEYS})
    if args.command == "config":
        result = config
    elif args.command == "notes-dir":
        result = {"notes_directory": config["notes_dir"]}
    else:
        client = Client(config["service_url"])
        if hasattr(args, "out_dir"):
            args.out_dir = args.out_dir.expanduser().resolve()
        if args.command == "transcribe":
            source = Path(args.source).expanduser()
            try:
                local = source.is_file() if not args.source.startswith(("http://", "https://")) else False
            except OSError:
                local = False
            payload = {"upload_task_id": client.upload(source)["upload_task_id"]} if local else {"video_url": args.source}
            task_id = client.request("POST", "/api/transcribe", payload)["task_id"]
            print(json.dumps({"task_id": task_id}), file=sys.stderr, flush=True)
            task = client.wait(task_id, args.wait)
            if task["status"] not in {"completed", "failed", "cancelled"}:
                raise TimeoutError(f"等待超时，服务继续处理；用 status/fetch 回读任务 {task_id}")
            result = client.fetch(task_id, args.out_dir,
                                  download_video=config["download_video"] and task["status"] == "completed")
            if task["status"] != "completed":
                print(json.dumps(result, ensure_ascii=False, indent=2))
                raise RuntimeError(task.get("error") or task["status"])
        elif args.command in {"frames", "fetch"}:
            if args.command == "frames":
                client.request("POST", f"/api/task/{args.task_id}/frames", {"timestamps": args.timestamps})
            result = client.fetch(args.task_id, args.out_dir, download_video=config["download_video"])
        elif args.command == "wait":
            result = client.wait(args.task_id, args.timeout)
        elif args.command == "tasks":
            result = client.request("GET", "/api/tasks?" + urllib.parse.urlencode({"offset": args.offset, "limit": args.limit}))
        elif args.command == "transcript":
            params = {"start": args.start, "offset": args.offset, "limit": args.limit, "output_format": args.output_format}
            if args.end is not None:
                params["end"] = args.end
            result = client.request("GET", f"/api/task/{args.task_id}/transcript?" + urllib.parse.urlencode(params))
        elif args.command == "health":
            result = client.request("GET", "/api/health")
        else:
            method = "POST" if args.command == "cancel" else "GET"
            suffix = "/cancel" if args.command == "cancel" else "/artifacts" if args.command == "artifacts" else ""
            result = client.request(method, f"/api/task/{args.task_id}{suffix}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError, http.client.HTTPException) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
