"""Serial queue lifecycle with durable status and bounded cancellation cleanup."""
from __future__ import annotations
import asyncio
from contextlib import suppress
from .asr.types import ASRError
from .service_pipeline import Pipeline
from .task_store import TERMINAL, TaskStore, public_error


class Jobs:
    def __init__(self, store: TaskStore, pipeline: Pipeline):
        self.store, self.pipeline = store, pipeline
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.active: asyncio.Task | None = None
        self.active_id: str | None = None
        self.worker: asyncio.Task | None = None
        self.stopping = False

    async def start(self) -> None:
        self.queue = asyncio.Queue()
        self.stopping = False
        for task_id in self.store.recover():
            self.queue.put_nowait(task_id)
        self.worker = asyncio.create_task(self.work())

    async def work(self) -> None:
        while True:
            task_id = await self.queue.get()
            try:
                if self.store.get(task_id)["status"] != "queued":
                    continue
                self.active_id = task_id
                self.store.update(task_id, status="processing")
                self.active = asyncio.create_task(self.pipeline.run(task_id, lambda: self.store.get(task_id)["status"] == "cancelled" or self.stopping))
                try:
                    await self.active
                except asyncio.CancelledError:
                    if self.stopping:
                        self.store.update(task_id, status="failed", phase="interrupted", error={"code": "interrupted", "message": "服务停止，已保留材料。"})
                        raise
                    self.store.update(task_id, status="cancelled", phase="cancelled")
                except Exception as error:
                    phase = self.store.get(task_id)["phase"]
                    self.store.update(task_id, status="failed", error={"code": error.code if isinstance(error, ASRError) else phase + "_failed", "message": public_error(error)})
            finally:
                self.active = None
                self.active_id = None
                self.queue.task_done()

    def enqueue(self, task_id: str) -> None:
        self.store.update(task_id, status="queued", phase="queued")
        self.queue.put_nowait(task_id)

    def cancel(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        if task["status"] in TERMINAL:
            return task
        task = self.store.update(task_id, status="cancelled", phase="cancelled")
        if self.active_id == task_id and self.active:
            self.active.cancel()
        return task

    async def stop(self) -> None:
        self.stopping = True
        if self.worker:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker

