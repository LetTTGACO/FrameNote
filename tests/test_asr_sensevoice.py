import asyncio
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from framenote.asr.sensevoice import SenseVoiceBackend
from framenote.asr.types import ASRError, ASRRequest
from framenote.service_settings import SenseVoiceConfig


@pytest.fixture
def backend(model_dirs, monkeypatch):
    model, vad = model_dirs
    monkeypatch.setattr("framenote.asr.sensevoice.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"versions":{"python":"test"}}', stderr=""))
    return SenseVoiceBackend(SenseVoiceConfig(model, vad, sys.executable))


@pytest.mark.asyncio
async def test_sensevoice_maps_raw_and_preserves_bytes(backend, tmp_path, monkeypatch):
    raw = {"engine":"SenseVoiceSmall-onnx-int8","timestamp_kind":"VAD_interval","duration":2,
           "segments":[{"start":0,"end":1,"text":"你好"}],"transcribe_seconds":.5,"versions":{"funasr-onnx":"fixture"}}
    original = json.dumps(raw, ensure_ascii=False).encode()
    async def run(args, **kwargs):
        Path(args[args.index("--output")+1]).write_bytes(original)
        kwargs["progress"]('{"completed":1,"total":2}')
        return b""
    monkeypatch.setattr("framenote.asr.sensevoice.run_process",run)
    audio=tmp_path/"audio.flac"
    audio.write_bytes(b"audio")
    progress=[]
    result=await backend.transcribe(ASRRequest(audio,2,tmp_path),on_progress=progress.append,cancelled=lambda:False)
    assert result.timestamp_kind == "VAD_interval"
    assert result.raw_path.read_bytes() == original
    assert result.segments[0].text == "你好"
    assert "load_seconds" not in result.metrics
    assert progress[0].completed == 1 and progress[0].total == 2
    original=b'{"segments":[{"text":"missing times"}]}'
    with pytest.raises(ASRError) as error:
        await backend.transcribe(ASRRequest(audio,2,tmp_path),on_progress=lambda p:None,cancelled=lambda:False)
    assert error.value.code == "asr_invalid_response"


def test_custom_weights_do_not_claim_locked_revision(backend):
    provenance=backend.describe()
    assert provenance.revision is None
    assert provenance.artifacts["sensevoice/model_quant.onnx"] == hashlib.sha256(b"test-model").hexdigest()
    assert provenance.parameters["language"] == "auto"
    assert provenance.parameters["textnorm"] == "withitn"
    assert provenance.parameters["batch_size"] == 1
    assert provenance.parameters["max_segment_seconds"] == 30
    assert provenance.parameters["vad_window_seconds"] == 300
    assert str(backend.config.model_dir) not in json.dumps(provenance.parameters)


def test_health_checks_all_assets_and_caches_python_probe(model_dirs, monkeypatch):
    model,vad=model_dirs
    calls=[]
    def probe(*args,**kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout='{"versions":{}}',stderr="")
    monkeypatch.setattr("framenote.asr.sensevoice.subprocess.run",probe)
    backend=SenseVoiceBackend(SenseVoiceConfig(model,vad,sys.executable))
    assert backend.health().available and backend.health().available
    assert len(calls)==1
    for directory in (model,vad):
        for path in list(directory.iterdir()):
            content=path.read_bytes()
            path.unlink()
            health=backend.health()
            assert not health.configured and not health.available and path.name in health.reason
            path.write_bytes(content)
    assert len(calls)==1
    monkeypatch.setattr("framenote.asr.sensevoice.subprocess.run",lambda *a,**k:SimpleNamespace(returncode=1,stdout="",stderr="ModuleNotFoundError: framenote"))
    broken=SenseVoiceBackend(SenseVoiceConfig(model,vad,sys.executable))
    health=broken.health()
    assert health.configured and not health.available and "framenote" in health.reason


@pytest.mark.asyncio
async def test_worker_command_keeps_parameters_and_spaced_python(model_dirs,tmp_path,monkeypatch):
    model,vad=model_dirs
    python=str(tmp_path/"python with spaces")
    monkeypatch.setattr("framenote.asr.sensevoice.subprocess.run",lambda *a,**k:SimpleNamespace(returncode=0,stdout='{"versions":{}}',stderr=""))
    backend=SenseVoiceBackend(SenseVoiceConfig(model,vad,python))
    async def run(args,**kwargs):
        assert args[0]==python and isinstance(args,list)
        assert kwargs["env"]["OMP_NUM_THREADS"]=="4"
        assert args[args.index("--threads")+1]=="4"
        Path(args[args.index("--output")+1]).write_text('{"engine":"sense","segments":[]}')
        return b""
    monkeypatch.setattr("framenote.asr.sensevoice.run_process",run)
    audio=tmp_path/"audio.flac"
    audio.touch()
    result=await backend.transcribe(ASRRequest(audio,2,tmp_path),on_progress=lambda p:None,cancelled=lambda:False)
    assert result.segments==[]
    with pytest.raises(asyncio.CancelledError):
        await backend.transcribe(ASRRequest(audio,2,tmp_path),on_progress=lambda p:None,cancelled=lambda:True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code",[(TimeoutError(),"asr_timeout"),(RuntimeError("failed"),"asr_failed"),(OSError("missing"),"asr_unavailable")])
async def test_worker_errors_keep_stable_codes(backend,tmp_path,monkeypatch,failure,code):
    async def run(*a,**k):
        raise failure
    monkeypatch.setattr("framenote.asr.sensevoice.run_process",run)
    audio=tmp_path/"audio.flac"
    audio.touch()
    with pytest.raises(ASRError) as error:
        await backend.transcribe(ASRRequest(audio,2,tmp_path),on_progress=lambda p:None,cancelled=lambda:False)
    assert error.value.code==code


@pytest.mark.asyncio
async def test_installed_backend_preserves_selected_python_search_path(tmp_path,monkeypatch):
    import subprocess
    from framenote.asr import sensevoice
    from framenote.asr.types import ASRProvenance,BackendHealth
    parent=tmp_path/"parent-site-packages"
    selected=tmp_path/"selected-python-modules"
    parent.mkdir()
    selected.mkdir()
    (parent/"asr_dependency.py").write_text('identity="parent"')
    (selected/"asr_dependency.py").write_text('identity="selected"')
    installed=parent/"framenote/asr/sensevoice.py"
    monkeypatch.setattr(sensevoice,"__file__",str(installed))
    monkeypatch.setenv("PYTHONPATH",str(selected))
    backend=SenseVoiceBackend(SenseVoiceConfig(tmp_path/"model",tmp_path/"vad",sys.executable))
    monkeypatch.setattr(backend,"health",lambda:BackendHealth("sensevoice",True,True,None))
    monkeypatch.setattr(backend,"describe",lambda:ASRProvenance("sensevoice","fixture",None))
    async def run(args,**kwargs):
        child=subprocess.run([sys.executable,"-c","import asr_dependency; print(asr_dependency.identity)"],env=kwargs["env"],cwd=tmp_path,capture_output=True,text=True)
        assert child.returncode==0,child.stderr
        assert child.stdout.strip()=="selected"
        Path(args[args.index("--output")+1]).write_text('{"engine":"fixture","segments":[]}')
        return b""
    monkeypatch.setattr(sensevoice,"run_process",run)
    audio=tmp_path/"audio.flac"
    audio.touch()
    await backend.transcribe(ASRRequest(audio,2,tmp_path),on_progress=lambda p:None,cancelled=lambda:False)


@pytest.mark.asyncio
async def test_actual_async_wait_timeout_keeps_stable_code(backend, tmp_path, monkeypatch):
    async def run(*args, **kwargs):
        return await asyncio.wait_for(asyncio.sleep(60), timeout=.001)

    monkeypatch.setattr("framenote.asr.sensevoice.run_process", run)
    audio = tmp_path / "audio.flac"
    audio.touch()
    with pytest.raises(ASRError) as error:
        await backend.transcribe(ASRRequest(audio, 2, tmp_path), on_progress=lambda p: None, cancelled=lambda: False)
    assert error.value.code == "asr_timeout"
