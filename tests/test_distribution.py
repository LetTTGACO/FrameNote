"""Opt-in distribution integration checks; share build and local wheel cache."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

pytestmark = [pytest.mark.packaging, pytest.mark.skipif(os.getenv("FRAMENOTE_RUN_PACKAGING") != "1", reason="Set FRAMENOTE_RUN_PACKAGING=1 for distribution checks")]


def checked(args, **kwargs):
    child=subprocess.run(args,capture_output=True,text=True,**kwargs)
    assert child.returncode==0,child.stdout+child.stderr
    return child


@pytest.fixture(scope="session")
def distributions(tmp_path_factory):
    root=Path(__file__).resolve().parents[1]
    output=tmp_path_factory.mktemp("distribution build")
    checked([sys.executable,"-m","build","--no-isolation","--outdir",str(output)],cwd=root)
    return next(output.glob("*.whl")),next(output.glob("*.tar.gz"))


def test_wheel_and_sdist_contents(distributions):
    wheel,source=distributions
    with zipfile.ZipFile(wheel) as archive:
        names=archive.namelist()
        assert "framenote/asr/models.lock.json" in names
        assert "framenote/service_mcp.py" not in names
        metadata = archive.read(next(name for name in names if name.endswith(".dist-info/METADATA"))).decode()
        assert "Requires-Dist: mcp" not in metadata
        assert any(name.endswith("/licenses/LICENSE") for name in names)
        assert any(name.endswith("/licenses/NOTICE") for name in names)
    with tarfile.open(source) as archive:
        names={name.split("/",1)[1] for name in archive.getnames() if "/" in name}
        expected={"skills/framenote/SKILL.md","skills/framenote/scripts/headless.py","deploy/framenote.service",
                  "deploy/service.env.example",".framenote.example.json","README.md","docs/MODELS.md","docs/ASR.md","LICENSE","NOTICE"}
        assert expected <= names,expected-names
        assert not any(part in {".local",".superpowers","workspace","models"} for name in names for part in Path(name).parts)
        assert not any(name.startswith("docs/superpowers/") for name in names)
        assert ".framenote.json" not in names
        assert not any(name.startswith(".codex/") for name in names)


def test_installed_wheel_runs_outside_checkout(distributions,tmp_path):
    wheel,_=distributions
    cache=Path(os.environ["FRAMENOTE_WHEELHOUSE"])
    assert cache.is_dir()
    venv=tmp_path/"new environment"
    checked([sys.executable,"-m","venv",str(venv)])
    python=venv/"bin/python"
    outside=tmp_path/"outside checkout"
    outside.mkdir()
    env={k:v for k,v in os.environ.items() if k!="PYTHONPATH"}
    env["PYTHONNOUSERSITE"]="1"
    checked([str(python),"-m","pip","install","--no-index","--find-links",str(cache),str(wheel)],cwd=outside,env=env)
    checked([str(python),"-m","pip","check"],cwd=outside,env=env)
    script="import json,sys,framenote; from framenote.models import required_assets; import framenote.service; print(json.dumps({'module':framenote.__file__,'counts':{k:len(v) for k,v in required_assets().items()},'heavy':[k for k in ('funasr_onnx','onnxruntime') if k in sys.modules]}))"
    child=checked([str(python),"-c",script],cwd=outside,env=env)
    probe=json.loads(child.stdout)
    assert Path(probe["module"]).is_relative_to(venv)
    assert probe["counts"]=={"sensevoice":6,"vad":4}
    assert probe["heavy"]==[]
    checked([str(python),"-m","framenote.asr_sensevoice","--help"],cwd=outside,env=env)
    child=subprocess.run([str(python),"-m","framenote.models","verify","--out","models"],cwd=outside,env=env,capture_output=True,text=True)
    assert child.returncode==1
    assert "chn_jpn_yue_eng_ko_spectok.bpe.model" in child.stderr
