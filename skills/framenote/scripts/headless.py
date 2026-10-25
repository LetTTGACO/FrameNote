#!/usr/bin/env python3
"""FrameNote HTTP client. Python standard library only; streamed file transfers."""
from __future__ import annotations

import argparse
from datetime import date as calendar_date
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

ARTIFACTS = {"metadata.json", "transcript.json", "transcript.md", "transcript.srt", "asr.raw.json", "frames.json",
             "subtitle.raw.json", "subtitle_attempts.json"}
CONFIG_KEYS = {"service_url", "notes_dir", "vault_dir", "download_video"}
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".ts", ".m4s"}


def check_version(payload: dict) -> None:
    version = payload.get("schema_version", 1)
    if type(version) is not int or version not in {1, 2, 3}:
        raise ValueError("Unsupported material schema version")


def is_material_name(name: str) -> bool:
    return name in ARTIFACTS or re.fullmatch(r"subtitle-[0-9]{4,}\.raw\.json", name) is not None


def check_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("等待时间必须是有限的非负数")


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
    for key in ("service_url", "notes_dir", "vault_dir"):
        if key in value and (not isinstance(value[key], str) or not value[key].strip()):
            raise ValueError(f"{key} 必须是非空字符串（{path}）")
    if "download_video" in value and type(value["download_video"]) is not bool:
        raise ValueError(f"download_video 必须是 true 或 false（{path}）")
    for key in ("notes_dir", "vault_dir"):
        if key in value:
            directory = Path(value[key]).expanduser()
            value[key] = str((directory if directory.is_absolute() else path.parent / directory).resolve())
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
    result: dict = {"service_url": "http://127.0.0.1:18121", "notes_dir": str(default_notes_dir(directory)),
              "vault_dir": str(Path.home() / "workspace/PersonalOS"), "download_video": False}
    sources = {key: "default" for key in result}
    for path in (user, project):
        if path is not None:
            values = read_config(path)
            result.update(values)
            sources.update({key: str(path) for key in values})
    environment = os.environ if environ is None else environ
    for key, variable in (("service_url", "FRAMENOTE_SERVICE_URL"), ("notes_dir", "FRAMENOTE_NOTES_DIR"),
                          ("vault_dir", "FRAMENOTE_VAULT_DIR"),
                          ("download_video", "FRAMENOTE_DOWNLOAD_VIDEO")):
        if variable in environment:
            result[key] = boolean(environment[variable]) if key == "download_video" else environment[variable]
            sources[key] = variable
    for key, value in overrides.items():
        if key in CONFIG_KEYS and value is not None:
            result[key] = value
            sources[key] = "command line"
    result["service_url"] = service_url(result["service_url"])
    for key in ("notes_dir", "vault_dir"):
        if not isinstance(result[key], (str, Path)) or not str(result[key]).strip():
            raise ValueError(f"{key} 必须是非空路径")
        path = Path(result[key]).expanduser()
        result[key] = str((path if path.is_absolute() else directory / path).resolve())
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


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Read scalar fields used by this client; leave body text and YAML lists alone."""
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        return {}, text
    fields = {}
    for key, raw in re.findall(r"^([a-z_]+):[ \t]*(.*)$", match[1], re.M):
        raw = raw.strip()
        if raw.startswith('"'):
            value, _ = json.JSONDecoder().raw_decode(raw)
            if not isinstance(value, str):
                continue
        elif raw.startswith("'") and raw.endswith("'"):
            value = raw[1:-1].replace("''", "'")
        else:
            value = raw.split(" #", 1)[0].strip()
        fields[key] = value
    return fields, text[match.end():]


def note_context(note: Path) -> dict:
    """Resolve new vault metadata or the previous relative bundle entry."""
    note = note.expanduser().resolve()
    fields, _ = split_frontmatter(note.read_text(encoding="utf-8"))
    if fields.get("framenote_task_id") and fields.get("framenote_service_url"):
        task_id, base = fields["framenote_task_id"], fields["framenote_service_url"]
    else:
        marker = note.parent / fields.get("bundle", "materials/bundle.json")
        bundle = json.loads(marker.read_text(encoding="utf-8"))
        check_version(bundle)
        task_id, base = bundle["task_id"], bundle["service_url"]
        fields.setdefault("source_url", bundle.get("task", {}).get("video_url", ""))
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise ValueError("笔记中的 FrameNote 任务 ID 无效")
    return {"note_path": str(note), "task_id": task_id, "service_url": service_url(base),
            "source_url": fields.get("source_url", "")}


def source_identity(source: str) -> str:
    """Match known platform identities without changing Bilibili part selection."""
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme not in {"http", "https"}:
        return str(Path(source).expanduser().resolve())
    host = (parsed.hostname or "").lower()
    query = urllib.parse.parse_qs(parsed.query)
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        video = (parsed.path.strip("/") if host == "youtu.be" else
                 query.get("v", [""])[0] if parsed.path == "/watch" else
                 parsed.path.split("/")[2] if parsed.path.startswith(("/shorts/", "/embed/", "/live/")) else "")
        if video:
            return "youtube:" + video
    if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
        match = re.search(r"/video/((?:BV|av)[A-Za-z0-9]+)", parsed.path)
        if match:
            return f"bilibili:{match[1]}:p={query.get('p', ['1'])[0]}"
    if host in {"douyin.com", "www.douyin.com", "m.douyin.com"}:
        match = re.search(r"/video/([0-9]+)", parsed.path)
        if match:
            return "douyin:" + match[1]
    pairs = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query)
             if not key.startswith("utm_") and key not in {"spm_id_from", "share_source", "vd_source"}]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"),
                                   urllib.parse.urlencode(sorted(pairs)), ""))


def vault_path(vault: Path, relative: str) -> Path:
    path = vault / relative
    if not path.resolve().is_relative_to(vault) or any(p.is_symlink() for p in (path, *path.parents) if p != vault):
        raise ValueError(f"知识库目标路径包含越界路径或符号链接：{relative}")
    return path


def find_notes(vault: Path, *, source_url: str = "", task_id: str = "", base: str = "") -> list[dict]:
    vault = vault.expanduser().resolve()
    if not vault.is_dir():
        raise ValueError(f"知识库目录不存在，请配置 vault_dir：{vault}")
    notes = vault_path(vault, "03_Bookmarks/Videos")
    found = []
    for note in sorted(notes.glob("*.md")):
        if note.is_symlink():
            continue
        fields, _ = split_frontmatter(note.read_text(encoding="utf-8"))
        same_source = bool(source_url and fields.get("source_url")
                           and source_identity(source_url) == source_identity(fields["source_url"]))
        same_task = bool(task_id and base and fields.get("framenote_task_id") == task_id
                         and fields.get("framenote_service_url", "").rstrip("/") == base.rstrip("/"))
        if same_source or same_task:
            found.append(note_context(note))
    return found


def export_note(output: Path, vault: Path, *, title: str, date: str, author: str,
                tags: list[str]) -> dict:
    """Export authored reading documents and selected frames, never replace vault edits."""
    output, vault = output.expanduser().resolve(), vault.expanduser().resolve()
    if not vault.is_dir():
        raise ValueError(f"知识库目录不存在，请配置 vault_dir：{vault}")
    if output.is_relative_to(vault):
        raise ValueError("材料工作区须位于知识库之外")
    calendar_date.fromisoformat(date)
    bundle = json.loads((output / "materials/bundle.json").read_text(encoding="utf-8"))
    check_version(bundle)
    task_id = bundle.get("task_id")
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise ValueError("材料任务 ID 无效")
    base = service_url(bundle["service_url"])
    draft = output / "notes.md"
    fields, body = split_frontmatter(draft.read_text(encoding="utf-8"))
    source = fields.get("source_url") or bundle.get("task", {}).get("video_url")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("请在 notes.md 的 source_url 中记录原视频来源")
    matches = find_notes(vault, source_url=source, task_id=task_id, base=base)
    if len(matches) > 1:
        raise ValueError("找到多份同视频笔记，请先核对：" + ", ".join(item["note_path"] for item in matches))
    if matches:
        old_note = Path(matches[0]["note_path"])
        old_raw = vault_path(vault, f"08_Attachments/Files/FrameNote/{old_note.stem}-原稿.md")
        return {**matches[0], "existing": True,
                "transcript_path": str(old_raw) if old_raw.is_file() else None,
                "message": "已有知识库笔记已保留，请直接在原笔记中继续讨论和维护原稿。"}
    if not bundle.get("download_complete") or bundle.get("task", {}).get("status") != "completed":
        raise ValueError("任务与材料须完成后再导出")
    created = fields.get("date") or fields.get("created") or date
    calendar_date.fromisoformat(created)
    name = re.sub(r'[\x00-\x1f/\\:*?"<>|#\[\]]+', " ", title).strip(" .")[:60].rstrip()
    if not name or not author.strip():
        raise ValueError("标题和整理者必须非空")
    stem = f"{created}-{name}"
    note_relative = f"03_Bookmarks/Videos/{stem}.md"
    raw_relative = f"08_Attachments/Files/FrameNote/{stem}-原稿.md"
    images_relative = f"08_Attachments/Images/videos/{stem}"
    # A readable name collision gets a stable task suffix, without renaming old notes.
    if any(vault_path(vault, rel).exists() for rel in (note_relative, raw_relative, images_relative)):
        stem += "-" + hashlib.sha256((base + ":" + task_id).encode()).hexdigest()[:8]
        note_relative = f"03_Bookmarks/Videos/{stem}.md"
        raw_relative = f"08_Attachments/Files/FrameNote/{stem}-原稿.md"
        images_relative = f"08_Attachments/Images/videos/{stem}"
    for relative in (note_relative, raw_relative, images_relative):
        if vault_path(vault, relative).exists():
            raise ValueError(f"目标已存在，请保留并核对：{relative}")
    raw_path = output / "chapters-transcript.md"
    _, raw_body = split_frontmatter(raw_path.read_text(encoding="utf-8"))
    corrections_path = output / "materials/corrections.md"
    corrections = corrections_path.read_text(encoding="utf-8") if corrections_path.exists() else ""
    if corrections:
        # Corrections become one section of the original transcript, retaining their wording.
        corrections = re.sub(r"^# .*(?:\n|$)", "", corrections, count=1)
        corrections = re.sub(r"^(#{2,5}) ", r"#\1 ", corrections, flags=re.M)
    body = re.sub(r"^# [^\n]+", lambda _: "# " + title, body, count=1, flags=re.M)
    raw_body = re.sub(r"^# [^\n]+", lambda _: "# " + title + "：分章原稿", raw_body, count=1, flags=re.M)
    documents = {"notes.md": body, "chapters-transcript.md": raw_body,
                 "materials/corrections.md": corrections}
    anchors = {}
    for filename, text in documents.items():
        anchors[filename] = dict(re.findall(r'<a\s+id=[\'"]([^\'"]+)[\'"]\s*></a>\s*\n#+\s+([^\n]+)', text))
    index_path = output / "materials/frames.json"
    frames = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    frame_index = {item["relative_path"]: item for item in frames}
    selected: dict[str, tuple[Path, str]] = {}
    used_names: set[str] = set()

    def convert_link(match: re.Match, filename: str) -> str:
        image, label, target = match.group(1), match.group(2), match.group(3).strip()
        target = re.sub(r'''\s+(?:"[^"]*"|'[^']*'|\([^)]*\))\s*$''', "", target)
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme in {"http", "https", "mailto"}:
            return match[0]
        if parsed.scheme:
            raise ValueError(f"请将本地材料链接替换为原稿或知识库链接：{target}")
        local = (output / filename).parent / urllib.parse.unquote(parsed.path) if parsed.path else output / filename
        local = local.resolve()
        if not local.is_relative_to(output):
            raise ValueError(f"阅读文档引用了工作区外的文件：{target}")
        relative = local.relative_to(output).as_posix()
        heading = urllib.parse.unquote(parsed.fragment)
        if relative in frame_index:
            if not re.fullmatch(r"materials/frames/[0-9a-f]{32}\.jpg", relative):
                raise ValueError(f"截图索引路径无效：{relative}")
            if not local.is_file():
                raise ValueError(f"截图不存在：{target}")
            if relative not in selected:
                seconds = float(frame_index[relative]["actual_time"])
                if not math.isfinite(seconds) or seconds < 0:
                    raise ValueError("截图实际时间无效")
                stamp = f"{int(seconds) // 60:02d}m{int(seconds) % 60:02d}s"
                candidate, number = stamp + ".jpg", 1
                while candidate in used_names:
                    number += 1
                    candidate = f"{stamp}-{number}.jpg"
                used_names.add(candidate)
                selected[relative] = (local, f"{images_relative}/{candidate}")
            destination = selected[relative][1]
        elif relative in {"notes.md", "chapters-transcript.md", "materials/corrections.md", "materials/transcript.md"}:
            key = "chapters-transcript.md" if relative == "materials/transcript.md" else relative
            heading = anchors.get(key, {}).get(heading, heading)
            destination = (note_relative if relative == "notes.md" else raw_relative)[:-3]
            if relative == "materials/corrections.md" and not heading:
                heading = "识别纠错与疑点"
            if relative == filename and heading:
                destination = ""
        else:
            raise ValueError(f"请移除原始材料链接，改用来源说明和任务入口回查：{target}")
        if any(char in heading for char in "|[]"):
            raise ValueError(f"章节标题需调整为可用于 Wikilink 的文字：{heading}")
        destination += "#" + heading if heading else ""
        return f"![[{destination}]]" if image else f"[[{destination}|{label}]]"

    def convert(text: str, filename: str) -> str:
        parts = re.split(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", text, flags=re.S)
        definitions = {}
        definition = r"^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*(.+)$"
        for part in parts[::2]:
            for key, destination in re.findall(definition, part, re.M):
                definitions[" ".join(key.split()).casefold()] = destination.strip()

        def expand_reference(match: re.Match) -> str:
            image, label, reference = match.groups()
            if match.string[match.end():].startswith("("):
                return match[0]
            key = " ".join((reference or label).split()).casefold()
            if key not in definitions:
                return match[0]
            return f"{image}[{label}]({definitions[key]})"

        def escape_alias(match: re.Match[str]) -> str:
            return "[[" + re.sub(r"(?<!\\)\|", r"\\|", match[1]) + "]]"

        for index in range(0, len(parts), 2):
            prose = re.sub(definition, "", parts[index], flags=re.M)
            prose = re.sub(r"(!?)(?<!\[)\[([^\[\]\n]+)\](?:\[([^\]\n]*)\])?", expand_reference, prose)
            parts[index] = re.sub(r"(!?)\[([^\]\n]*)\]\((<[^>\n]+>(?:[ \t]+[^)\n]*)?|[^)\n]+)\)",
                                  lambda match: convert_link(match, filename), prose)
        lines = "".join(parts).splitlines(keepends=True)
        table_rows: set[int] = set()
        fence = False
        for index, line in enumerate(lines):
            if line.lstrip().startswith(("```", "~~~")):
                fence = not fence
            if not fence and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", line):
                table_rows.add(index - 1)
                row = index + 1
                while row < len(lines) and "|" in lines[row] and lines[row].strip():
                    table_rows.add(row)
                    row += 1
        for row in table_rows:
            segments = re.split(r"(`[^`\n]*`)", lines[row])
            for index in range(0, len(segments), 2):
                segments[index] = re.sub(r"\[\[([^\]\n]+)\]\]",
                                         escape_alias, segments[index])
            lines[row] = "".join(segments)
        return "".join(lines)

    body = convert(body, "notes.md")
    raw_body = convert(raw_body, "chapters-transcript.md")
    corrections = convert(corrections, "materials/corrections.md")
    if corrections:
        raw_body = raw_body.rstrip() + "\n\n## 识别纠错与疑点\n\n" + corrections.strip() + "\n"
    planned = {note_relative[:-3]: body, raw_relative[:-3]: raw_body}
    copied = {rel for _, rel in selected.values()}
    for current, text in planned.items():
        # Only prose is link-bearing; quoted commands and examples stay verbatim.
        prose = "".join(re.split(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", text, flags=re.S)[::2])
        for target in re.findall(r"\[\[([^\]\n]+)\]\]", prose):
            path, _, heading = target.replace(r"\|", "|").split("|", 1)[0].partition("#")
            if path in copied:
                continue
            linked = path.removesuffix(".md") if path else current
            if linked in planned:
                linked_body = planned[linked]
            else:
                candidate = vault_path(vault, linked)
                if not candidate.is_file():
                    candidate = vault_path(vault, linked + ".md")
                if not candidate.is_file():
                    raise ValueError(f"知识库链接目标不存在：{target}")
                linked_body = candidate.read_text(encoding="utf-8") if heading else ""
            if heading and heading not in re.findall(r"^#{1,6} (.+?)\s*#*\s*$", linked_body, re.M):
                raise ValueError(f"知识库章节链接不存在：{target}")
    metadata = {"title": title, "date": created, "updated": date, "source": author, "type": "summary",
                "status": "active", "tags": tags, "source_url": source,
                "framenote_task_id": task_id, "framenote_service_url": base}

    def document(text: str, properties: dict) -> str:
        header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in properties.items())
        return "---\n" + header + "\n---\n\n" + text.lstrip()

    note_text = document(body, metadata)
    raw_text = document(raw_body, {**metadata, "title": title + "：分章原稿", "type": "note"})
    pending = [(vault_path(vault, rel), path.read_bytes()) for path, rel in selected.values()]
    pending.extend([(vault_path(vault, raw_relative), raw_text.encode()),
                    (vault_path(vault, note_relative), note_text.encode())])
    written: list[Path] = []
    created_directories: list[Path] = []
    try:
        for path, data in pending:
            missing = []
            directory = path.parent
            while not directory.exists():
                missing.append(directory)
                directory = directory.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_directories.append(directory)
            with path.open("xb") as stream:
                written.append(path)
                stream.write(data)
    except OSError:
        # Roll back only files created by this invocation; existing notes remain untouched.
        for path in reversed(written):
            path.unlink(missing_ok=True)
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass  # Another writer may now own files here; only remove empty directories.
        raise
    return {**note_context(vault / note_relative), "transcript_path": str(vault / raw_relative),
            "images": [rel for _, rel in selected.values()], "existing": False}


class Client:
    def __init__(self, base: str):
        self.base = service_url(base)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, data: dict | None = None, *, timeout: float = 120) -> dict:
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=timeout) as response:
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
            if error.code == 304 and headers and previous is not None:
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

    def wait_operation(self, task_id: str, operation_id: str, *, timeout: float) -> dict:
        """Wait on a public media operation; timeout returns the latest operation."""
        check_timeout(timeout)
        deadline = time.monotonic() + timeout
        latest = None
        while True:
            remaining = max(0, deadline - time.monotonic())
            query = urllib.parse.urlencode({"wait": min(45, remaining)})
            try:
                latest = self.request("GET", f"/api/task/{task_id}/operations/{operation_id}?{query}",
                                      timeout=min(50, max(.001, remaining)) if timeout else 1)
            except (TimeoutError, urllib.error.URLError) as error:
                if isinstance(error, urllib.error.URLError) and not isinstance(error.reason, TimeoutError):
                    raise
                if latest is not None:
                    return latest
                raise TimeoutError(f"媒体等待超时，后台继续；用 operation {task_id} {operation_id} 继续查询") from None
            if latest["status"] in {"completed", "failed", "cancelled"} or time.monotonic() >= deadline:
                return latest
            time.sleep(min(.2, max(0, deadline - time.monotonic())))

    def _finish_operation(self, task_id: str, operation: dict, timeout: float) -> dict:
        operation_id = operation["operation_id"]
        if operation["status"] not in {"completed", "failed", "cancelled"}:
            operation = self.wait_operation(task_id, operation_id, timeout=timeout)
        if operation["status"] in {"failed", "cancelled"}:
            raise RuntimeError(f"媒体操作 {operation_id}: {operation.get('error') or operation['status']}")
        if operation["status"] != "completed":
            raise TimeoutError(f"媒体等待超时，后台继续；用 operation {task_id} {operation_id} 继续查询，或重试原命令")
        return operation

    def request_frames(self, task_id: str, timestamps: list[float], *, timeout: float = 1800) -> dict:
        """Request frames and return the completed public operation payload."""
        check_timeout(timeout)
        operation = self.request("POST", f"/api/task/{task_id}/frames",
                                 {"timestamps": timestamps, "background": True})
        return self._finish_operation(task_id, operation, timeout)

    def prepare_video(self, task_id: str, *, timeout: float = 1800) -> dict:
        """Prepare playable media and return the completed public operation payload."""
        check_timeout(timeout)
        operation = self.request("POST", f"/api/task/{task_id}/media", {"purpose": "video"})
        return self._finish_operation(task_id, operation, timeout)

    def fetch(self, task_id: str, output: Path, *, download_video: bool = False,
              media_wait: float = 1800) -> dict:
        check_timeout(media_wait)
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
            elif directory.exists() and any(is_material_name(path.name) for path in directory.iterdir()):
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
        bundle = {"schema_version": max(2, manifest.get("schema_version", 1)), "service_url": self.base, "task_id": task_id, "task": task,
                  "directory": str(output.resolve()), "materials_directory": str(materials.resolve()),
                  "download_video": download_video, "download_complete": False}
        if "video" in previous:
            bundle["video"] = previous["video"]
        write_json(bundle_path, bundle)
        for item in manifest["artifacts"]:
            name = item["name"]
            if is_material_name(name):
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
            self.prepare_video(task_id, timeout=media_wait)
            task = self.request("GET", f"/api/task/{task_id}")
            if task.get("task_id") != task_id:
                raise ValueError("Service returned a different task")
            bundle["task"] = task
            bundle["video"] = self.retrieve_video(task, output, previous.get("video"))
        bundle["download_complete"] = True
        write_json(bundle_path, bundle)
        return bundle

    def wait(self, task_id: str, timeout: float) -> dict:
        check_timeout(timeout)
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
    common.add_argument("--vault-dir", default=argparse.SUPPRESS)
    common.add_argument("--download-video", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS,
                        help="取回服务端保留的视频；--no-download-video 覆盖自动取回配置")
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    commands = parser.add_subparsers(dest="command", required=True)
    def command(name, **kwargs):
        return commands.add_parser(name, parents=[common], **kwargs)
    transcribe = command("transcribe")
    transcribe.add_argument("source", help="B站/抖音/YouTube 链接或本地视频路径")
    transcribe.add_argument("--wait", type=float, default=1800)
    transcribe.add_argument("--force-asr", action="store_true")
    transcribe.add_argument("--out-dir", type=Path, required=True, help="笔记根目录；材料归入 materials/")
    frames = command("frames")
    frames.add_argument("task_id")
    frames.add_argument("--timestamps", type=float, nargs="+", required=True)
    frames.add_argument("--out-dir", type=Path, required=True)
    fetch = command("fetch")
    fetch.add_argument("task_id")
    fetch.add_argument("--out-dir", type=Path, required=True)
    frames.add_argument("--wait", type=float, default=1800)
    fetch.add_argument("--wait", type=float, default=1800)
    operation = command("operation")
    operation.add_argument("task_id")
    operation.add_argument("operation_id")
    operation.add_argument("--wait", type=float, default=45)
    cancel_operation = command("cancel-operation")
    cancel_operation.add_argument("task_id")
    cancel_operation.add_argument("operation_id")
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
    export = command("export", help="将阅读稿、分章原稿与精选截图保存到 PersonalOS")
    export.add_argument("--out-dir", type=Path, required=True, help="含 notes.md 和 materials/ 的工作区")
    export.add_argument("--title", required=True)
    export.add_argument("--date", required=True, help="用户时区的 YYYY-MM-DD 日期")
    export.add_argument("--author", default="codex")
    export.add_argument("--tags", nargs="*", default=[])
    lookup = command("find-note", help="按原视频来源查找知识库中已有笔记")
    lookup.add_argument("source_url")
    context = command("note-context", help="从知识库笔记或旧版 bundle 入口读取任务身份")
    context.add_argument("note", type=Path)
    args = parser.parse_args()
    config = resolve_config(**{key: getattr(args, key, None) for key in CONFIG_KEYS})
    if args.command == "config":
        result = config
    elif args.command == "notes-dir":
        result = {"notes_directory": config["notes_dir"], "vault_directory": config["vault_dir"]}
    elif args.command == "find-note":
        result = {"notes": find_notes(Path(config["vault_dir"]), source_url=args.source_url)}
    elif args.command == "note-context":
        result = note_context(args.note)
    elif args.command == "export":
        result = export_note(args.out_dir, Path(config["vault_dir"]), title=args.title,
                             date=args.date, author=args.author, tags=args.tags)
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
            payload["force_asr"] = args.force_asr
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
                client.request_frames(args.task_id, args.timestamps, timeout=args.wait)
            result = client.fetch(args.task_id, args.out_dir, download_video=config["download_video"],
                                  media_wait=args.wait)
        elif args.command == "operation":
            result = client.wait_operation(args.task_id, args.operation_id, timeout=args.wait)
        elif args.command == "cancel-operation":
            result = client.request("POST", f"/api/task/{args.task_id}/operations/{args.operation_id}/cancel")
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
