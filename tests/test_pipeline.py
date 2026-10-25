import asyncio
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

from framenote.asr.types import ASRError, ASRProvenance, ASRResult, ASRProgress, BackendHealth
from framenote.service import create_app, Jobs
from framenote.service_pipeline import Pipeline
from framenote.service_settings import Settings, SenseVoiceConfig
from framenote.task_store import TaskStore
from framenote.transcript import TranscriptSegment


class FakeBackend:
    def describe(self):
        return ASRProvenance(backend="fixture",model="fixture",revision=None)
    def health(self):
        return BackendHealth("fixture",True,True,None)
    async def transcribe(self,request,*,on_progress,cancelled):
        path=request.work_dir/"fixture-original.json"
        path.write_bytes(b'{ "fixture": "raw" }')
        return ASRResult(segments=[TranscriptSegment(0,1,"识别原文")],timestamp_kind="segment_interval",
                         provenance=self.describe(),raw_path=path)


@pytest.fixture
def setup_pipeline(tmp_path,video):
    settings=Settings(tmp_path/"workspace",SenseVoiceConfig(tmp_path/"model",tmp_path/"vad",sys.executable))
    store=TaskStore(settings.workspace/"tasks")
    task=store.create(upload_name="source.mp4",title="fixture",status="processing")
    task_id=task["task_id"]
    directory=store.directory(task_id)
    (directory/"source.mp4").write_bytes(video.read_bytes())
    return settings,store,task_id,directory


@pytest.mark.asyncio
async def test_pipeline_accepts_model_independent_backend(setup_pipeline):
    settings,store,task_id,directory=setup_pipeline
    await Pipeline(settings,store,FakeBackend()).run(task_id,lambda:False)
    payload=json.loads((directory/"transcript.json").read_text())
    assert payload["schema_version"]==2
    assert payload["timestamp_kind"]=="segment_interval"
    assert payload["provenance"]["backend"]=="fixture"
    task=store.get(task_id)
    assert task["result"]["asr_seconds"]>=0
    assert "model_load_seconds" not in task["result"]
    assert task["asr_run"]["backend"]=="fixture"
    assert (directory/"asr.raw.json").read_bytes()==b'{ "fixture": "raw" }'
    assert task["status"]=="completed" and task["progress"]==100
    assert "VAD" not in (directory/"transcript.md").read_text()


@pytest.mark.asyncio
async def test_progress_retains_last_valid_percentage(setup_pipeline):
    settings,store,task_id,directory=setup_pipeline
    observed=[]
    class Reporting(FakeBackend):
        async def transcribe(self,request,*,on_progress,cancelled):
            for completed,total in [(1,2),(None,None),(-1,2),(3,2),(0,0)]:
                on_progress(ASRProgress(completed,total))
                observed.append(store.get(task_id)["progress"])
            return await super().transcribe(request,on_progress=on_progress,cancelled=cancelled)
    await Pipeline(settings,store,Reporting()).run(task_id,lambda:False)
    assert observed==[62,62,62,62,62]
    assert store.get(task_id)["progress"]==100


@pytest.mark.asyncio
async def test_pipeline_cancel_at_backend_return_never_publishes(setup_pipeline):
    settings,store,task_id,directory=setup_pipeline
    entered,release=asyncio.Event(),asyncio.Event()
    aborted=False
    class Blocking(FakeBackend):
        async def transcribe(self,request,**kwargs):
            entered.set()
            await release.wait()
            return await super().transcribe(request,**kwargs)
    running=asyncio.create_task(Pipeline(settings,store,Blocking()).run(task_id,lambda:aborted))
    await asyncio.wait_for(entered.wait(),3)
    aborted=True
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not (directory/"transcript.json").exists()
    assert store.get(task_id)["status"]!="completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("symlink",[False,True])
async def test_pipeline_rejects_raw_outside_work_dir(setup_pipeline,tmp_path,symlink):
    settings,store,task_id,directory=setup_pipeline
    outside=tmp_path/"outside.json"
    outside.write_bytes(b"private")
    class Escaping(FakeBackend):
        async def transcribe(self,request,**kwargs):
            result=await super().transcribe(request,**kwargs)
            from dataclasses import replace
            raw=outside
            if symlink:
                raw=directory/"symlink.json"
                raw.symlink_to(outside)
            return replace(result,raw_path=raw)
    with pytest.raises(ASRError) as error:
        await Pipeline(settings,store,Escaping()).run(task_id,lambda:False)
    assert error.value.code=="asr_invalid_response"
    assert not (directory/"asr.raw.json").exists()
    assert outside.read_bytes()==b"private"


@pytest.mark.asyncio
async def test_pipeline_silence_completes(setup_pipeline):
    settings,store,task_id,directory=setup_pipeline
    class Silent(FakeBackend):
        async def transcribe(self,request,**kwargs):
            from dataclasses import replace
            return replace(await super().transcribe(request,**kwargs),segments=[])
    await Pipeline(settings,store,Silent()).run(task_id,lambda:False)
    payload=json.loads((directory/"transcript.json").read_text())
    assert store.get(task_id)["status"]=="completed"
    assert payload["segments"]==[] and "no_speech_detected" in payload["warnings"]


@pytest.mark.asyncio
async def test_jobs_waits_cleanup_and_preserves_asr_error_codes(tmp_path):
    store=TaskStore(tmp_path)
    first,last=[store.create()["task_id"] for _ in range(2)]
    entered,cleanup,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
    ran=[]
    class Runner:
        async def run(self,task_id,aborted):
            ran.append(task_id)
            if task_id==first:
                entered.set()
                try:
                    await asyncio.sleep(30)
                finally:
                    cleanup.set()
                    await release.wait()
            store.update(task_id,status="completed")
    jobs=Jobs(store,Runner())
    await jobs.start()
    try:
        jobs.enqueue(first)
        jobs.enqueue(last)
        await asyncio.wait_for(entered.wait(),2)
        jobs.cancel(first)
        await asyncio.wait_for(cleanup.wait(),2)
        assert ran==[first]
        release.set()
        await asyncio.wait_for(jobs.queue.join(),2)
        assert ran==[first,last]
    finally:
        release.set()
        await jobs.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("code",["asr_unavailable","asr_timeout","asr_invalid_response","asr_failed"])
async def test_jobs_preserve_each_asr_code(tmp_path,code):
    store=TaskStore(tmp_path)
    task_id=store.create()["task_id"]
    class Broken:
        async def run(self,*args):
            raise ASRError(code,"safe failure")
    jobs=Jobs(store,Broken())
    await jobs.start()
    try:
        jobs.enqueue(task_id)
        await asyncio.wait_for(jobs.queue.join(),2)
        assert store.get(task_id)["error"]["code"]==code
    finally:
        await jobs.stop()


def test_health_and_ready_distinguish_liveness(tmp_path,model_dirs,monkeypatch):
    model,vad=model_dirs
    settings=Settings(tmp_path/"workspace",SenseVoiceConfig(model,vad,sys.executable))
    from types import SimpleNamespace
    monkeypatch.setattr("framenote.asr.sensevoice.subprocess.run",lambda *a,**k:SimpleNamespace(returncode=0,stdout='{"versions":{}}',stderr=""))
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code==200
        assert client.get("/api/ready").status_code==200
        (model/"chn_jpn_yue_eng_ko_spectok.bpe.model").unlink()
        assert client.get("/api/health").status_code==200
        assert client.get("/api/ready").status_code==503
        assert not client.get("/api/health").json()["asr_models_present"]


def test_workspace_second_instance_rejected(tmp_path):
    settings=Settings.from_env({},base_dir=tmp_path)
    with TestClient(create_app(settings)):
        with pytest.raises(RuntimeError,match="Workspace already in use"):
            with TestClient(create_app(settings)):
                pass


@pytest.mark.asyncio
async def test_jobs_stop_recovers_queue_and_completed_materials(tmp_path):
    store=TaskStore(tmp_path)
    active,queued,completed=[store.create()["task_id"] for _ in range(3)]
    store.update(completed,status="completed")
    raw=store.directory(completed)/"transcript.json"
    raw.write_bytes(b"preserve")
    entered=asyncio.Event()
    class Runner:
        async def run(self,task_id,aborted):
            entered.set()
            await asyncio.sleep(30)
    jobs=Jobs(store,Runner())
    await jobs.start()
    jobs.enqueue(active)
    jobs.enqueue(queued)
    await asyncio.wait_for(entered.wait(),2)
    await jobs.stop()
    assert store.get(active)["status"]=="failed"
    assert store.get(active)["phase"]=="interrupted"
    assert store.recover()==[queued]
    assert raw.read_bytes()==b"preserve"


@pytest.fixture
def subtitle_pipeline(setup_pipeline, monkeypatch):
    from framenote.subtitles import BilibiliIdentity, SubtitleChoice, SubtitleOutcome, SubtitleTrack, SubtitleAttempt
    settings, store, task_id, directory = setup_pipeline
    original_url = 'https://b23.tv/example?p=2'
    store.update(task_id, upload_name=None, video_url=original_url)
    identity = BilibiliIdentity('https://www.bilibili.com/video/BV1cfaf6SEA9?p=2',
                                'BV1cfaf6SEA9', 123, 2, 456, '第二部分', 5., '字幕视频')
    raw = b'{"body":[{"from":0,"to":1,"content":"subtitle"}]}'
    (directory / 'subtitle-0001.raw.json').write_bytes(raw)
    choice = SubtitleChoice([TranscriptSegment(0, 1, '字幕')], SubtitleTrack('track', 'zh-CN', False, None),
                            'subtitle-0001.raw.json', [], {'coverage_ratio': .2}, '2026-10-25T00:00:00+00:00')
    outcome = SubtitleOutcome(identity, choice, [SubtitleAttempt('parse', 'track', 'selected', None,
                                                               'subtitle-0001.raw.json')], None)
    calls = {'media': [], 'asr': []}
    class RecordingBackend(FakeBackend):
        async def transcribe(self, request, **kwargs):
            calls['asr'].append(request)
            return await super().transcribe(request, **kwargs)
    pipeline = Pipeline(settings, store, RecordingBackend())
    async def subtitles(*args):
        return outcome
    async def upstream(task_id, url):
        calls['media'].append(url)
        return directory / 'source.mp4', directory / 'source.mp4', {'title': '媒体视频', 'source': 'bilibili'}
    monkeypatch.setattr(pipeline, 'acquire_subtitles', subtitles, raising=False)
    monkeypatch.setattr(pipeline, 'acquire_upstream', upstream)
    return pipeline, store, task_id, directory, identity, outcome, calls


@pytest.mark.asyncio
async def test_subtitle_success_skips_all_media_and_asr(subtitle_pipeline, monkeypatch):
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    async def no_probe(*args):
        pytest.fail('subtitle-only completion must not probe media')
    monkeypatch.setattr('framenote.service_pipeline.probe_media', no_probe)
    await pipeline.run(task_id, lambda: False)
    task = store.get(task_id)
    assert calls == {'media': [], 'asr': []}
    assert task['status'] == 'completed' and task['duration'] == identity.duration
    assert task['material_schema_version'] == 3
    assert 'asr_run' not in task and 'asr_seconds' not in task['result']
    assert task['result']['source_kind'] == 'platform_subtitle'
    assert task['result']['timestamp_kind'] == 'subtitle_interval'
    assert task['result']['segment_count'] == 1
    assert task['result']['total_seconds'] >= task['result']['preparation_seconds'] >= 0
    assert not task.get('assets') and not (directory / 'asr.raw.json').exists()
    payload = json.loads((directory / 'transcript.json').read_text())
    assert payload['provenance']['fetched_at'] == outcome.choice.fetched_at
    assert payload['provenance']['identity']['cid'] == identity.cid
    metadata = json.loads((directory / 'metadata.json').read_text())
    assert metadata['duration_source'] == 'platform_part'
    assert metadata['source_identity']['page'] == 2 and metadata['completeness'] == 'unknown'
    assert metadata['source_url'] == store.get(task_id)['video_url']
    assert (directory / 'subtitle.raw.json').read_bytes() == (directory / 'subtitle-0001.raw.json').read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,outcome_name,error_code', [
    ('no_tracks', 'empty', None), ('auth_required', 'error', 'auth_required'),
    ('network_error', 'error', 'network_error'), ('all_candidates_invalid', 'invalid', 'invalid_response'),
])
async def test_failed_subtitle_lookup_runs_existing_asr_and_keeps_reason(subtitle_pipeline, monkeypatch,
                                                                        reason, outcome_name, error_code):
    from dataclasses import replace
    from framenote.subtitles import SubtitleAttempt
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    async def subtitles(*args):
        return replace(outcome, choice=None, fallback_reason=reason,
                       attempts=[SubtitleAttempt('tracks', None, outcome_name, error_code)])
    monkeypatch.setattr(pipeline, 'acquire_subtitles', subtitles)
    await pipeline.run(task_id, lambda: False)
    task = store.get(task_id)
    assert len(calls['asr']) == 1 and calls['media'] == [identity.canonical_url]
    assert task['video_url'] == 'https://b23.tv/example?p=2'
    assert task['status'] == 'completed' and task['fallback_reason'] == reason
    attempt = json.loads((directory / 'subtitle_attempts.json').read_text())[-1]
    assert attempt['outcome'] == outcome_name and attempt['error_code'] == error_code
    metadata = json.loads((directory / 'metadata.json').read_text())
    assert metadata['duration_source'] == 'media_probe'
    assert metadata['platform_duration'] == identity.duration and metadata['source_identity']['cid'] == identity.cid
    assert json.loads((directory / 'transcript.json').read_text())['schema_version'] == 2
    assert (directory / 'subtitle-0001.raw.json').exists()


@pytest.mark.asyncio
async def test_force_asr_never_fetches_subtitles(subtitle_pipeline, monkeypatch):
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    store.update(task_id, force_asr=True)
    async def forbidden(*args):
        pytest.fail('force_asr must skip subtitle acquisition')
    monkeypatch.setattr(pipeline, 'acquire_subtitles', forbidden)
    await pipeline.run(task_id, lambda: False)
    assert len(calls['media']) == len(calls['asr']) == 1
    assert store.get(task_id)['material_schema_version'] == 3
    assert not (directory / 'subtitle_attempts.json').exists()


@pytest.mark.asyncio
async def test_cancel_at_subtitle_return_never_falls_back(subtitle_pipeline, monkeypatch):
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    aborted = False
    async def subtitles(*args):
        nonlocal aborted
        aborted = True
        return outcome
    monkeypatch.setattr(pipeline, 'acquire_subtitles', subtitles)
    with pytest.raises(asyncio.CancelledError):
        await pipeline.run(task_id, lambda: aborted)
    assert calls == {'media': [], 'asr': []}
    assert not (directory / 'transcript.json').exists()
    assert not (directory / 'subtitle_attempts.json').exists()


@pytest.mark.asyncio
async def test_invalid_page_worker_error_fails_without_asr(subtitle_pipeline, monkeypatch):
    from framenote.subtitles import BilibiliInputError
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    monkeypatch.delattr(pipeline, 'acquire_subtitles')
    async def worker(*args, **kwargs):
        (directory / 'subtitle-acquisition.json').write_text(json.dumps({
            'error': {'code': 'invalid_video_part', 'message': 'Invalid page'}}))
        return b''
    monkeypatch.setattr('framenote.service_pipeline.run_process', worker)
    with pytest.raises(BilibiliInputError) as error:
        await pipeline.run(task_id, lambda: False)
    assert error.value.code == 'invalid_video_part'
    assert calls == {'media': [], 'asr': []}


@pytest.mark.asyncio
@pytest.mark.parametrize('known_identity', [False, True])
async def test_unsafe_target_only_fails_when_identity_resolution_was_unsafe(subtitle_pipeline, monkeypatch, known_identity):
    from dataclasses import replace
    from framenote.subtitles import BilibiliInputError, SubtitleAttempt
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    async def subtitles(*args):
        return replace(outcome, identity=identity if known_identity else None, choice=None,
                       fallback_reason='unsafe_target', attempts=[SubtitleAttempt('download' if known_identity else 'resolve',
                                                                                None, 'error', 'unsafe_target')])
    monkeypatch.setattr(pipeline, 'acquire_subtitles', subtitles)
    if known_identity:
        await pipeline.run(task_id, lambda: False)
        assert calls['media'] == [identity.canonical_url] and len(calls['asr']) == 1
    else:
        with pytest.raises(BilibiliInputError) as error:
            await pipeline.run(task_id, lambda: False)
        assert error.value.code == 'invalid_video_url' and calls == {'media': [], 'asr': []}


@pytest.mark.asyncio
async def test_asr_failure_keeps_subtitle_raw(subtitle_pipeline, monkeypatch):
    from dataclasses import replace
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    async def subtitles(*args):
        return replace(outcome, choice=None, fallback_reason='all_candidates_invalid')
    async def broken(*args, **kwargs):
        raise ASRError('asr_failed', 'fixture failure')
    monkeypatch.setattr(pipeline, 'acquire_subtitles', subtitles)
    monkeypatch.setattr(pipeline.backend, 'transcribe', broken)
    raw = (directory / 'subtitle-0001.raw.json').read_bytes()
    with pytest.raises(ASRError):
        await pipeline.run(task_id, lambda: False)
    assert (directory / 'subtitle-0001.raw.json').read_bytes() == raw
    assert (directory / 'subtitle_attempts.json').is_file()
    assert not (directory / 'transcript.json').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['https://www.douyin.com/video/123', 'https://www.youtube.com/watch?v=BaW_jenozKc', None])
async def test_non_bilibili_routes_keep_existing_acquisition(subtitle_pipeline, monkeypatch, url):
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    store.update(task_id, video_url=url, upload_name=None if url else 'source.mp4')
    async def forbidden(*args):
        pytest.fail('other input must skip subtitles')
    monkeypatch.setattr(pipeline, 'acquire_subtitles', forbidden)
    await pipeline.run(task_id, lambda: False)
    assert len(calls['asr']) == 1 and calls['media'] == ([url] if url else [])
    assert 'material_schema_version' not in store.get(task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['full', 'subtitles'])
@pytest.mark.parametrize('configured', [False, True])
async def test_acquisition_children_use_explicit_settings(subtitle_pipeline, tmp_path, monkeypatch, mode, configured):
    from dataclasses import replace
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    monkeypatch.setenv('FRAMENOTE_BILIBILI_CREDENTIALS', '/unrelated/inherited.json')
    monkeypatch.setenv('FRAMENOTE_PROCESS_TIMEOUT', '9999')
    credentials = tmp_path / 'private.json' if configured else None
    pipeline.settings = replace(pipeline.settings, bilibili_credentials=credentials, process_timeout=42.5)
    monkeypatch.delattr(pipeline, 'acquire_subtitles')
    monkeypatch.delattr(pipeline, 'acquire_upstream')
    seen = []
    async def worker(args, **kwargs):
        seen.append((args, kwargs))
        if mode == 'subtitles':
            (directory / 'subtitle-acquisition.json').write_text(json.dumps(outcome.to_payload()))
        else:
            (directory / 'acquisition.json').write_text(json.dumps({
                'video': 'source.mp4', 'audio': 'source.mp4', 'metadata': {}, 'notes': []}))
        return b''
    monkeypatch.setattr('framenote.service_pipeline.run_process', worker)
    if mode == 'subtitles':
        assert (await pipeline.acquire_subtitles(task_id, lambda: False)).identity == identity
    else:
        await pipeline.acquire_upstream(task_id, identity.canonical_url)
    args, kwargs = seen[0]
    assert '--mode' in args and args[args.index('--mode') + 1] == mode
    assert kwargs['timeout'] == 42.5 and kwargs['env']['FRAMENOTE_PROCESS_TIMEOUT'] == '42.5'
    assert kwargs['env'].get('FRAMENOTE_BILIBILI_CREDENTIALS') == (str(credentials) if configured else None)
    if configured:
        assert str(credentials) not in args


@pytest.mark.asyncio
async def test_subtitle_worker_timeout_falls_back_without_swallowing_cancel(subtitle_pipeline, monkeypatch):
    from framenote.service_process import run_process
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    monkeypatch.delattr(pipeline, 'acquire_subtitles')
    async def worker(args, **kwargs):
        if '--mode' in args:
            raise TimeoutError('private upstream detail')
        return await run_process(args, **kwargs)
    monkeypatch.setattr('framenote.service_pipeline.run_process', worker)
    await pipeline.run(task_id, lambda: False)
    assert len(calls['asr']) == 1 and store.get(task_id)['fallback_reason'] == 'deadline_exceeded'
    attempt = json.loads((directory / 'subtitle_attempts.json').read_text())[-1]
    assert attempt['outcome'] == 'error' and attempt['error_code'] == 'deadline_exceeded'
    assert 'private upstream detail' not in (directory / 'task.json').read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['invalid_video_part', 'invalid_video_url'])
async def test_jobs_preserve_bilibili_input_error_from_worker(subtitle_pipeline, monkeypatch, code):
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    monkeypatch.delattr(pipeline, 'acquire_subtitles')
    async def worker(*args, **kwargs):
        (directory / 'subtitle-acquisition.json').write_text(json.dumps({
            'error': {'code': code, 'message': 'Invalid Bilibili input'}}))
        return b''
    monkeypatch.setattr('framenote.service_pipeline.run_process', worker)
    jobs = Jobs(store, pipeline)
    await jobs.start()
    try:
        jobs.enqueue(task_id)
        await asyncio.wait_for(jobs.queue.join(), 2)
        assert store.get(task_id)['status'] == 'failed'
        assert store.get(task_id)['error']['code'] == code
        assert calls == {'media': [], 'asr': []}
    finally:
        await jobs.stop()


@pytest.mark.asyncio
async def test_subtitle_worker_cancellation_never_falls_back(subtitle_pipeline, monkeypatch):
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    monkeypatch.delattr(pipeline, 'acquire_subtitles')
    async def worker(*args, **kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr('framenote.service_pipeline.run_process', worker)
    with pytest.raises(asyncio.CancelledError):
        await pipeline.run(task_id, lambda: False)
    assert calls == {'media': [], 'asr': []}
    assert not (directory / 'subtitle_attempts.json').exists()


@pytest.mark.asyncio
async def test_subtitle_fallback_galaxy_uses_fixed_identity(subtitle_pipeline, monkeypatch):
    from dataclasses import replace
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    pipeline.settings = replace(pipeline.settings, galaxy_enabled=True)
    async def subtitles(*args):
        return replace(outcome, choice=None, fallback_reason='no_tracks')
    async def upstream(*args):
        raise RuntimeError('upstream unavailable')
    async def galaxy(self, url, task_directory):
        calls['media'].append(url)
        return directory / 'source.mp4', directory / 'source.mp4', {'title': 'Galaxy 视频', 'source': 'bilibili'}
    monkeypatch.setattr(pipeline, 'acquire_subtitles', subtitles)
    monkeypatch.setattr(pipeline, 'acquire_upstream', upstream)
    monkeypatch.setattr('framenote.service_pipeline.GalaxyDownloader.download', galaxy)
    await pipeline.run(task_id, lambda: False)
    assert calls['media'] == [identity.canonical_url] and len(calls['asr']) == 1
    assert store.get(task_id)['status'] == 'completed'


@pytest.mark.parametrize('code', ['invalid_video_part', 'invalid_video_url'])
def test_http_preserves_worker_bilibili_input_error(subtitle_pipeline, monkeypatch, code):
    import time
    pipeline, store, task_id, directory, identity, outcome, calls = subtitle_pipeline
    monkeypatch.delattr(pipeline, 'acquire_subtitles')
    async def worker(args, **kwargs):
        submitted_id = args[args.index('--task-id') + 1]
        (store.directory(submitted_id) / 'subtitle-acquisition.json').write_text(json.dumps({
            'error': {'code': code, 'message': 'Invalid Bilibili input'}}))
        return b''
    monkeypatch.setattr('framenote.service_pipeline.run_process', worker)
    with TestClient(create_app(pipeline.settings, pipeline)) as client:
        submitted = client.post('/api/transcribe', json={'video_url': identity.canonical_url}).json()['task_id']
        for _ in range(100):
            task = client.get(f'/api/task/{submitted}').json()
            if task['status'] == 'failed':
                break
            time.sleep(.01)
        assert task['status'] == 'failed' and task['error']['code'] == code
        assert calls == {'media': [], 'asr': []}
