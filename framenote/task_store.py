"""Atomic, per-task manifests. Files remain the source of truth after restarts."""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TERMINAL = {"completed", "failed", "cancelled"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def public_error(error: BaseException) -> str:
    # Signed media URLs and upstream query values must not enter task logs.
    return re.sub(r"(https?://[^\s?'\"]+)\?[^\s'\"]*", r"\1?[redacted]", str(error))[:1000]


class TaskStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, task_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise KeyError("Invalid task id")
        path = self.root / task_id
        if path.is_symlink() or path.resolve().parent != self.root:
            raise KeyError("Invalid task directory")
        return path

    def create(self, **values: Any) -> dict:
        task_id = uuid.uuid4().hex
        task = {"task_id": task_id, "status": "uploaded", "phase": "uploaded", "progress": 0,
                "created_at": now(), "updated_at": now(), "logs": [], **values}
        atomic_json(self.directory(task_id) / "task.json", task)
        return task

    def get(self, task_id: str) -> dict:
        try:
            path = self.directory(task_id) / "task.json"
            if path.is_symlink():
                raise KeyError("Invalid task manifest")
            return json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise KeyError("Task not found") from exc

    def update(self, task_id: str, **values: Any) -> dict:
        task = self.get(task_id)
        task.update(values, updated_at=now())
        atomic_json(self.directory(task_id) / "task.json", task)
        return task

    def log(self, task_id: str, message: str) -> None:
        task = self.get(task_id)
        logs = [*task["logs"], {"at": now(), "message": message}][-100:]
        self.update(task_id, logs=logs)

    def all(self) -> list[dict]:
        tasks = []
        for path in self.root.glob("*/task.json"):
            if not re.fullmatch(r"[0-9a-f]{32}", path.parent.name):
                continue
            try:
                tasks.append(self.get(path.parent.name))
            except KeyError:
                continue
        return sorted(tasks, key=lambda t: t["created_at"], reverse=True)

    def recover(self) -> list[str]:
        queued = []
        for task in reversed(self.all()):
            if task["status"] == "queued":
                queued.append(task["task_id"])
            elif task["status"] == "processing":
                self.update(task["task_id"], status="failed", phase="interrupted",
                            error={"code": "interrupted", "message": "服务停止时任务未完成；已落盘材料保留，可重新提交。"})
        return queued
