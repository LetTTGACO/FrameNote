from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from framenote.jobs import Jobs
from framenote.media_jobs import MediaJobs
from framenote.service_process import run_process
from framenote.service_video import fingerprint
from framenote.task_store import TaskStore, atomic_json


class FakePipeline:
    def __init__(self, store):
        self.store = store
        self.settings = SimpleNamespace(process_timeout=10)
        self.downloads = 0
        self.active = 0
        self.maximum = 0

    async def prepare_media(self, task_id, *, require_audio, cancelled):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            path = self.store.directory(task_id) / 'source.mp4'
            if not path.exists():
                self.downloads += 1
                path.write_bytes(b'video')
            await asyncio.sleep(.01)
            atomic_json(path.parent / 'media.json', {'video': path.name, 'download': path.name,
                        'source_has_audio': False, 'fingerprints': {'video': fingerprint(path), 'download': fingerprint(path)}})
            return path
        finally:
            self.active -= 1

    async def run(self, task_id, cancelled):
        await self.prepare_media(task_id, require_audio=False, cancelled=cancelled)
        self.store.update(task_id, status='completed')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / 'tasks')
    task_id = store.create(status='completed', phase='completed', duration=5)['task_id']
    directory = store.directory(task_id)
    (directory / 'transcript.json').write_bytes(b'original text')
    (directory / 'subtitle.raw.json').write_bytes(b'original subtitle')
    pipe = FakePipeline(store)
    async def extract(video, output, seconds, *, timeout):
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(b'frame')
        return {'requested_time': seconds, 'actual_time': seconds + .02, 'timestamp_kind': 'decoded_frame_pts', 'bytes': 5, 'sha256': 'fake'}
    monkeypatch.setattr('framenote.media_jobs.extract_frame_isolated', extract)
    return store, task_id, pipe, MediaJobs(store, pipe, asyncio.Lock())


@pytest.mark.asyncio
async def test_same_request_coalesces_and_different_operations_share_video(setup):
    store, task_id, pipe, media = setup
    jobs = Jobs(store, pipe, gate=media.gate)
    await jobs.start()
    await media.start()
    try:
        first = media.enqueue_frames(task_id, [1.0])
        assert media.enqueue_frames(task_id, [1])['operation_id'] == first['operation_id']
        video = media.enqueue_video(task_id)
        jobs.enqueue(store.create()['task_id'])
        assert (await media.get(task_id, first['operation_id'], wait=45))['status'] == 'completed'
        assert (await media.get(task_id, video['operation_id'], wait=45))['status'] == 'completed'
        await jobs.queue.join()
        assert pipe.downloads == 2  # one for each task; frames/video share one
        assert pipe.maximum == 1
    finally:
        await media.stop()
        await jobs.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('implicit', [False, True])
async def test_retry_inherits_completed_frames_and_only_runs_missing(setup, monkeypatch, implicit):
    store, task_id, pipe, media = setup
    original = __import__('framenote.media_jobs', fromlist=['extract_frame_isolated']).extract_frame_isolated
    calls = []
    async def extract(video, output, seconds, *, timeout):
        calls.append(seconds)
        if len(calls) == 2:
            raise RuntimeError('decode failure')
        return await original(video, output, seconds, timeout=timeout)
    monkeypatch.setattr('framenote.media_jobs.extract_frame_isolated', extract)
    await media.start()
    try:
        op = media.enqueue_frames(task_id, [1, 2])
        failed = await media.get(task_id, op['operation_id'], wait=45)
        assert failed['status'] == 'failed'
        first_frame = failed['result']['frames'][0]
        retry = media.enqueue_frames(task_id, [1, 2]) if implicit else media.retry(task_id, op['operation_id'])
        assert retry['retry_of'] == op['operation_id'] and retry['operation_id'] != op['operation_id']
        done = await media.get(task_id, retry['operation_id'], wait=45)
        assert done['status'] == 'completed'
        assert done['result']['frames'][0] == first_frame
        assert calls == [1, 2, 2]
        assert (await media.get(task_id, op['operation_id'])) == failed
    finally:
        await media.stop()


@pytest.mark.asyncio
async def test_cached_frames_keep_actual_pts_and_invalidate_changed_media(setup):
    store, task_id, pipe, media = setup
    await media.start()
    try:
        op = media.enqueue_frames(task_id, [1])
        done = await media.get(task_id, op['operation_id'], wait=45)
        assert media.enqueue_frames(task_id, [1]) == done
        assert done['result']['frames'][0]['actual_time'] == 1.02
        (store.directory(task_id) / 'source.mp4').write_bytes(b'changed video')
        assert media.enqueue_frames(task_id, [1])['operation_id'] != op['operation_id']
    finally:
        await media.stop()


@pytest.mark.asyncio
async def test_media_interruption_never_changes_completed_text(setup, monkeypatch):
    store, task_id, pipe, media = setup
    directory = store.directory(task_id)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    entered = asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(pipe, 'prepare_media', blocked)
    await media.start()
    op = media.enqueue_video(task_id)
    await entered.wait()
    await media.stop()
    restarted = MediaJobs(store, pipe, asyncio.Lock())
    await restarted.start()
    try:
        interrupted = await restarted.get(task_id, op['operation_id'])
        assert interrupted['status'] == 'failed'
        assert interrupted['error']['code'] == 'interrupted'
        assert {name: (directory / name).read_bytes() for name in before} == before
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_restart_resumes_queued_marks_processing_and_skips_cancelled(setup):
    store, task_id, pipe, media = setup
    queued = media.enqueue_frames(task_id, [1])
    cancelled = media.enqueue_frames(task_id, [2])
    media.cancel(task_id, cancelled['operation_id'])
    interrupted = media.enqueue_video(task_id)
    path = store.directory(task_id) / 'operations' / (interrupted['operation_id'] + '.json')
    value = json.loads(path.read_text())
    value['status'] = 'processing'
    atomic_json(path, value)
    restarted = MediaJobs(store, pipe, asyncio.Lock())
    await restarted.start()
    try:
        assert (await restarted.get(task_id, queued['operation_id'], wait=45))['status'] == 'completed'
        assert (await restarted.get(task_id, cancelled['operation_id']))['status'] == 'cancelled'
        assert (await restarted.get(task_id, interrupted['operation_id']))['error']['code'] == 'interrupted'
        assert pipe.downloads == 1
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_operation_cannot_cross_task_or_follow_symlink(setup, tmp_path):
    store, task_id, pipe, media = setup
    op = media.enqueue_video(task_id)
    other = store.create(status='completed')['task_id']
    with pytest.raises(KeyError):
        await media.get(other, op['operation_id'])
    operations = store.directory(other) / 'operations'
    operations.symlink_to(store.directory(task_id) / 'operations', target_is_directory=True)
    with pytest.raises(KeyError):
        await media.get(other, op['operation_id'])
    with pytest.raises(KeyError):
        media.enqueue_video(other)
    path = store.directory(task_id) / 'operations' / (op['operation_id'] + '.json')
    content = path.read_bytes()
    path.unlink()
    outside = tmp_path / 'private.json'
    outside.write_bytes(content)
    path.symlink_to(outside)
    with pytest.raises(KeyError):
        await media.get(task_id, op['operation_id'])


@pytest.mark.asyncio
async def test_cancel_waits_for_frame_process_cleanup_before_next_job(setup, monkeypatch, tmp_path):
    store, task_id, pipe, media = setup
    marker = tmp_path / 'writes'
    entered = asyncio.Event()
    from framenote.frame_extractor import extract_frame_isolated
    async def decoder_process(args, **kwargs):
        pending = args[args.index('--output') + 1]
        def ready(line):
            entered.set()
        return await run_process([sys.executable, '-u', '-c',
            "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); output=pathlib.Path(sys.argv[2]); "
            "output.write_bytes(b'partial'); output.with_suffix('.tmp').write_bytes(b'temporary'); print('ready',flush=True)\n"
            "while True:\n with p.open('ab') as f: f.write(b'x')\n time.sleep(.005)", str(marker), pending], progress=ready)
    original_prepare = pipe.prepare_media
    async def prepare(*args, **kwargs):
        if kwargs['require_audio']:
            size = marker.stat().st_size
            await asyncio.sleep(.08)
            assert marker.stat().st_size == size, 'decoder still writing when next heavy operation started'
            assert not list((store.directory(task_id) / 'frames').iterdir())
        return await original_prepare(*args, **kwargs)
    monkeypatch.setattr(pipe, 'prepare_media', prepare)
    monkeypatch.setattr('framenote.service_process.run_process', decoder_process)
    monkeypatch.setattr('framenote.media_jobs.extract_frame_isolated', extract_frame_isolated)
    await media.start()
    try:
        op = media.enqueue_frames(task_id, [1])
        await asyncio.wait_for(entered.wait(), 5)
        media.cancel(task_id, op['operation_id'])
        next_op = media.enqueue_video(task_id)
        assert (await media.get(task_id, next_op['operation_id'], wait=45))['status'] == 'completed'
        size = marker.stat().st_size
        await asyncio.sleep(.08)
        assert marker.stat().st_size == size
        assert (await media.get(task_id, op['operation_id']))['status'] == 'cancelled'
        assert store.get(task_id)['status'] == 'completed'
    finally:
        await media.stop()


@pytest.mark.asyncio
async def test_cancel_queued_does_not_acquire(setup):
    store, task_id, pipe, media = setup
    op = media.enqueue_video(task_id)
    media.cancel(task_id, op['operation_id'])
    await media.start()
    try:
        await media.queue.join()
        assert pipe.downloads == 0
    finally:
        await media.stop()


@pytest.mark.asyncio
async def test_retry_keeps_other_frames_when_one_completed_image_is_missing(setup, monkeypatch):
    store, task_id, pipe, media = setup
    original = __import__('framenote.media_jobs', fromlist=['extract_frame_isolated']).extract_frame_isolated
    calls = []
    async def extract(video, output, seconds, *, timeout):
        calls.append(seconds)
        if len(calls) == 3:
            raise RuntimeError('failed third frame')
        return await original(video, output, seconds, timeout=timeout)
    monkeypatch.setattr('framenote.media_jobs.extract_frame_isolated', extract)
    await media.start()
    try:
        op = media.enqueue_frames(task_id, [1, 2, 3])
        failed = await media.get(task_id, op['operation_id'], wait=45)
        first, second = failed['result']['frames']
        (store.directory(task_id) / 'frames' / (first['frame_id'] + '.jpg')).unlink()
        retry = media.retry(task_id, op['operation_id'])
        assert retry['result']['frames'] == [second]
        done = await media.get(task_id, retry['operation_id'], wait=45)
        assert done['status'] == 'completed'
        assert done['result']['frames'][1] == second
        assert calls == [1, 2, 3, 1, 3]
    finally:
        await media.stop()


@pytest.mark.asyncio
async def test_get_wait_is_bounded_and_does_not_cancel_operation(setup):
    store, task_id, pipe, media = setup
    op = media.enqueue_video(task_id)
    value = await asyncio.wait_for(media.get(task_id, op['operation_id'], wait=.01), timeout=1)
    assert value['status'] == 'queued' and pipe.downloads == 0


@pytest.mark.asyncio
async def test_completed_operation_with_missing_image_is_not_cached(setup):
    store, task_id, pipe, media = setup
    await media.start()
    try:
        op = media.enqueue_frames(task_id, [1])
        done = await media.get(task_id, op['operation_id'], wait=45)
        frame = done['result']['frames'][0]
        (store.directory(task_id) / 'frames' / (frame['frame_id'] + '.jpg')).unlink()
        assert media.enqueue_frames(task_id, [1])['operation_id'] != op['operation_id']
    finally:
        await media.stop()


@pytest.mark.asyncio
async def test_isolated_decoder_uses_service_package_outside_source_cwd(tmp_path, video, monkeypatch):
    from framenote.frame_extractor import extract_frame_isolated
    monkeypatch.chdir(tmp_path)
    output = tmp_path / 'frames' / 'one.jpg'
    result = await extract_frame_isolated(video, output, 1.2, timeout=10)
    assert output.read_bytes().startswith(b'\xff\xd8')
    assert abs(result['actual_time'] - 1.2) < .041
    assert result['timestamp_kind'] == 'decoded_frame_pts'
    assert list(output.parent.iterdir()) == [output]


@pytest.mark.asyncio
@pytest.mark.parametrize('stale', [False, True])
@pytest.mark.parametrize('status', ['failed', 'cancelled'])
async def test_implicit_retry_chooses_descendant_and_invalidates_stale_frames(setup, monkeypatch, stale, status):
    store, task_id, pipe, media = setup
    original = __import__('framenote.media_jobs', fromlist=['extract_frame_isolated']).extract_frame_isolated
    calls = []
    async def extract(video, output, seconds, *, timeout):
        calls.append(seconds)
        if seconds == 2:
            raise RuntimeError('decode failure')
        return await original(video, output, seconds, timeout=timeout)
    monkeypatch.setattr('framenote.media_jobs.extract_frame_isolated', extract)
    await media.start()
    try:
        first = media.enqueue_frames(task_id, [1, 2])
        failed = await media.get(task_id, first['operation_id'], wait=45)
        child = media.retry(task_id, first['operation_id'])
        child_failed = await media.get(task_id, child['operation_id'], wait=45)
        if status == 'cancelled':
            saved = media._read(task_id, child['operation_id'])
            saved.update(status='cancelled', phase='cancelled')
            media._save(saved)
            child_failed = await media.get(task_id, child['operation_id'])
        if stale:
            (store.directory(task_id) / 'source.mp4').write_bytes(b'new media')
        retry = media.enqueue_frames(task_id, [1, 2])
        assert retry['retry_of'] == child['operation_id']
        assert retry['result']['frames'] == ([] if stale else failed['result']['frames'])
        assert media.enqueue_frames(task_id, [1, 2]) == retry
        done = await media.get(task_id, retry['operation_id'], wait=45)
        assert calls == ([1, 2, 2, 1, 2] if stale else [1, 2, 2, 2])
        assert await media.get(task_id, child['operation_id']) == child_failed
    finally:
        await media.stop()
