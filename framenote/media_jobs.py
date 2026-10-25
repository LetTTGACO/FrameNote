"""Durable media operations independent of completed transcript manifests."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import math
from pathlib import Path
import re
import uuid

from .frame_extractor import extract_frame_isolated
from .service_pipeline import Pipeline
from .service_video import fingerprint, media_manifest, retained_file, retained_video
from .task_store import TERMINAL, TaskStore, atomic_json, public_error


class MediaJobs:
    def __init__(self, store: TaskStore, pipeline: Pipeline, gate: asyncio.Lock):
        self.store, self.pipeline, self.gate = store, pipeline, gate
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.active: asyncio.Task | None = None
        self.active_key: tuple[str, str] | None = None
        self.stopping = False
        self.events: dict[tuple[str, str], asyncio.Event] = {}

    def _directory(self, task_id: str) -> Path:
        self.store.get(task_id)
        path = self.store.directory(task_id) / 'operations'
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise KeyError('Invalid operations directory')
        return path

    def _path(self, task_id: str, operation_id: str) -> Path:
        if not re.fullmatch(r'[0-9a-f]{32}', operation_id):
            raise KeyError('Invalid operation id')
        path = self._directory(task_id) / (operation_id + '.json')
        if path.is_symlink():
            raise KeyError('Invalid operation file')
        return path

    def _read(self, task_id: str, operation_id: str) -> dict:
        try:
            value = json.loads(self._path(task_id, operation_id).read_text())
        except (FileNotFoundError, ValueError) as error:
            raise KeyError('Operation not found') from error
        if not isinstance(value, dict) or value.get('task_id') != task_id or value.get('operation_id') != operation_id:
            raise KeyError('Operation not found')
        return value

    def _all(self, task_id: str) -> list[dict]:
        values = []
        for path in sorted(self._directory(task_id).glob('*.json')):
            try:
                values.append(self._read(task_id, path.stem))
            except KeyError:
                continue
        return values

    @staticmethod
    def _public(value: dict) -> dict:
        return {key: item for key, item in value.items() if not key.startswith('_')}

    def summaries(self, task_id: str) -> list[dict]:
        return [{key: value[key] for key in ('operation_id', 'kind', 'status', 'phase', 'error')}
                for value in self._all(task_id)]

    def _save(self, value: dict) -> None:
        atomic_json(self._path(value['task_id'], value['operation_id']), value)
        if value['status'] in TERMINAL:
            event = self.events.pop((value['task_id'], value['operation_id']), None)
            if event:
                event.set()

    def _fingerprints(self, task_id: str, kind: str) -> dict:
        directory = self.store.directory(task_id)
        video = retained_video(directory, self.store.get(task_id))
        result = {'video': fingerprint(video)} if video else {}
        if kind == 'video':
            manifest = media_manifest(directory)
            for name in ('audio', 'download'):
                path = retained_file(directory, manifest.get(name))
                if path:
                    result[name] = fingerprint(path)
        return result

    def _frame_exists(self, task_id: str, frame: dict) -> bool:
        directory = self.store.directory(task_id) / 'frames'
        frame_id = frame.get('frame_id', '')
        if directory.is_symlink() or not isinstance(frame_id, str) or not re.fullmatch(r'[0-9a-f]{32}', frame_id):
            return False
        path = directory / (frame_id + '.jpg')
        return not path.is_symlink() and path.is_file() and path.stat().st_size == frame.get('bytes')

    def _cache_valid(self, value: dict) -> bool:
        saved = value.get('_fingerprints')
        return bool(saved) and saved == self._fingerprints(value['task_id'], value['kind']) and all(
            self._frame_exists(value['task_id'], frame) for frame in value['result'].get('frames', []))

    def _enqueue(self, task_id: str, kind: str, request: dict, *, retry: dict | None = None) -> dict:
        matching = [value for value in self._all(task_id) if value['kind'] == kind and value['request'] == request]
        for value in matching:
            if value['status'] not in TERMINAL:
                return self._public(value)
        if retry is None:
            for value in matching:
                if value['status'] == 'completed' and self._cache_valid(value):
                    return self._public(value)
        value = {'operation_id': uuid.uuid4().hex, 'task_id': task_id, 'kind': kind,
                 'status': 'queued', 'phase': 'queued', 'request': request,
                 'result': {'frames': []} if kind == 'frames' else {}, 'error': None}
        if retry:
            value['retry_of'] = retry['operation_id']
            if kind == 'frames' and retry.get('_fingerprints') == self._fingerprints(task_id, kind):
                value['result'] = {'frames': [frame for frame in retry['result']['frames']
                                              if self._frame_exists(task_id, frame)]}
                value['_fingerprints'] = retry.get('_fingerprints', {})
        self._save(value)
        self.queue.put_nowait((task_id, value['operation_id']))
        return self._public(value)

    def enqueue_frames(self, task_id: str, timestamps: list[float]) -> dict:
        task = self.store.get(task_id)
        duration = task.get('duration', 0)
        if not 1 <= len(timestamps) <= 24 or any(not math.isfinite(t) or t < 0 or t >= duration for t in timestamps):
            raise ValueError('Frame timestamps must be within the video duration (1–24 points)')
        return self._enqueue(task_id, 'frames', {'timestamps': [float(t) for t in timestamps]})

    def enqueue_video(self, task_id: str) -> dict:
        return self._enqueue(task_id, 'video', {'purpose': 'video'})

    async def get(self, task_id: str, operation_id: str, *, wait: float = 0) -> dict:
        if not math.isfinite(wait) or not 0 <= wait <= 45:
            raise ValueError('wait must be within 0–45 seconds')
        value = self._read(task_id, operation_id)
        if wait and value['status'] not in TERMINAL:
            key = (task_id, operation_id)
            event = self.events.setdefault(key, asyncio.Event())
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=wait)
            value = self._read(task_id, operation_id)
        return self._public(value)

    def cancel(self, task_id: str, operation_id: str) -> dict:
        value = self._read(task_id, operation_id)
        if value['status'] not in TERMINAL:
            value.update(status='cancelled', phase='cancelled')
            self._save(value)
            if self.active_key == (task_id, operation_id) and self.active:
                self.active.cancel()
        return self._public(value)

    def retry(self, task_id: str, operation_id: str) -> dict:
        value = self._read(task_id, operation_id)
        if value['status'] not in {'failed', 'cancelled'}:
            raise ValueError('Only failed or cancelled operations can be retried')
        return self._enqueue(task_id, value['kind'], value['request'], retry=value)

    async def start(self) -> None:
        self.queue = asyncio.Queue()
        self.stopping = False
        for task in reversed(self.store.all()):
            try:
                operations = self._all(task['task_id'])
            except KeyError:
                continue
            for value in operations:
                if value['status'] == 'queued':
                    self.queue.put_nowait((task['task_id'], value['operation_id']))
                elif value['status'] == 'processing':
                    value.update(status='failed', phase='interrupted', error={'code': 'interrupted', 'message': '服务中断，已完成媒体材料保留。'})
                    self._save(value)
        self.worker = asyncio.create_task(self._work())

    async def stop(self) -> None:
        self.stopping = True
        if self.worker:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker

    async def _work(self) -> None:
        while True:
            key = await self.queue.get()
            try:
                async with self.gate:
                    value = self._read(*key)
                    if value['status'] != 'queued':
                        continue
                    value.update(status='processing', phase='media_preparation')
                    self._save(value)
                    self.active_key = key
                    self.active = asyncio.create_task(self._run(value))
                    try:
                        await self.active
                    except asyncio.CancelledError:
                        current = self._read(*key)
                        if current['status'] != 'cancelled':
                            current.update(status='failed', phase='interrupted', error={'code': 'interrupted', 'message': '服务停止，已完成媒体材料保留。'})
                            self._save(current)
                        if self.stopping:
                            raise
                    except Exception as error:
                        current = self._read(*key)
                        if current['status'] != 'cancelled':
                            code = getattr(error, 'code', None)
                            if code is None:
                                code = 'audio_unavailable' if str(error) == 'audio_unavailable' else current['phase'] + '_failed'
                            current.update(status='failed', error={'code': code, 'message': public_error(error)})
                            self._save(current)
            finally:
                self.active = None
                self.active_key = None
                self.queue.task_done()

    async def _run(self, value: dict) -> None:
        task_id, operation_id = value['task_id'], value['operation_id']
        def cancelled() -> bool:
            return self.stopping or self._read(task_id, operation_id)['status'] == 'cancelled'
        video = await self.pipeline.prepare_media(task_id, require_audio=value['kind'] == 'video', cancelled=cancelled)
        if cancelled():
            raise asyncio.CancelledError
        current_fingerprints = self._fingerprints(task_id, value['kind'])
        if value.get('_fingerprints') != current_fingerprints and value['kind'] == 'frames':
            value['result'] = {'frames': []}
        value['_fingerprints'] = current_fingerprints
        value['phase'] = 'frames' if value['kind'] == 'frames' else 'video'
        self._save(value)
        if value['kind'] == 'frames':
            directory = self.store.directory(task_id)
            frames_directory = directory / 'frames'
            index_path = directory / 'frames.json'
            if frames_directory.is_symlink() or index_path.is_symlink():
                raise ValueError('Invalid frame storage')
            index = json.loads(index_path.read_text()) if index_path.is_file() else []
            inherited = list(value['result']['frames'])
            completed: list[dict] = []
            value['result']['frames'] = completed
            for timestamp in value['request']['timestamps']:
                existing = next((frame for frame in inherited if frame['requested_time'] == timestamp), None)
                if existing is not None:
                    completed.append(existing)
                    inherited.remove(existing)
                    continue
                frame_id = uuid.uuid4().hex
                item = await extract_frame_isolated(video, frames_directory / (frame_id + '.jpg'), timestamp,
                                                    timeout=self.pipeline.settings.process_timeout)
                item.update(frame_id=frame_id, url=f'/api/task/{task_id}/frames/{frame_id}')
                index.append(item)
                atomic_json(index_path, index)
                completed.append(item)
                value['result']['frames'] = completed + inherited
                self._save(value)
            value['result']['frames'] = completed
        else:
            value['result'] = {'downloads': {'video': f'/api/task/{task_id}/video'}}
        value.update(status='completed', phase='completed')
        self._save(value)
