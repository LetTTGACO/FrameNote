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
