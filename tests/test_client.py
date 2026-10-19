import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("headless_client", Path(__file__).resolve().parents[1] / "skills/framenote/scripts/headless.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize("subdirectory", ["", "materials"])
def test_fetch_rejects_cross_task_directory_before_mutating_it(tmp_path, monkeypatch, subdirectory):
    materials = tmp_path / subdirectory
    materials.mkdir(exist_ok=True)
    (materials / "bundle.json").write_text(json.dumps({"task_id": "A"}))
    (materials / "transcript.json").write_text("original task A")
    client = module.Client("http://127.0.0.1:18121")
    monkeypatch.setattr(client, "request", lambda *a, **k: pytest.fail("Must check ownership before network operations"))
    with pytest.raises(ValueError, match="其他任务"):
        client.fetch("B", tmp_path)
    assert (materials / "transcript.json").read_text() == "original task A"


def test_fetch_only_downloads_frames_in_current_manifest_and_marks_partial_attempt(tmp_path, monkeypatch):
    client = module.Client("http://127.0.0.1:18121")
    def request(method, path):
        if path.endswith("/artifacts"):
            return {"artifacts": []}
        return {"task_id": "A", "status": "processing"}
    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr(client, "download", lambda *a: pytest.fail("No artifact download expected"))
    result = client.fetch("A", tmp_path)
    assert result["download_complete"]
    (tmp_path / "materials/frames.json").write_text('[{"frame_id":"old"}]')
    assert client.fetch("A", tmp_path)["download_complete"]


@pytest.mark.parametrize("version", [None, 2])
def test_v1_v2_refetch_preserves_reading_documents(tmp_path, monkeypatch, version):
    client = module.Client("http://127.0.0.1:18121")
    frame_ids = ["a" * 32]
    notes = "# Notes\n\n![frame](materials/frames/" + frame_ids[0] + ".jpg)\n"
    (tmp_path / "notes.md").write_text(notes)
    (tmp_path / "chapters-transcript.md").write_text("Original chapter transcript")

    def request(method, path):
        if path.endswith("/artifacts"):
            return {**({"schema_version":version,"task_id":"A"} if version else {}), "artifacts": [{"name": "frames.json"}, {"name": "transcript.md"}]}
        return {"task_id": "A", "status": "completed"}

    def download(route, path):
        bundle = json.loads((tmp_path / "materials/bundle.json").read_text())
        assert not bundle["download_complete"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.name == "frames.json":
            path.write_text(json.dumps([{"frame_id": value} for value in frame_ids]))
        else:
            path.write_text(route)

    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr(client, "download", download)
    result = client.fetch("A", tmp_path)
    corrections = tmp_path / "materials/corrections.md"
    corrections.write_text("Terminology evidence")
    frame_ids.append("b" * 32)
    result = client.fetch("A", tmp_path)
    assert result["directory"] == str(tmp_path.resolve())
    assert result["materials_directory"] == str((tmp_path / "materials").resolve())
    assert result["download_complete"]
    assert result["schema_version"] == 2
    assert (tmp_path / "notes.md").read_text() == notes
    assert (tmp_path / "chapters-transcript.md").read_text() == "Original chapter transcript"
    assert corrections.read_text() == "Terminology evidence"
    frames = json.loads((tmp_path / "materials/frames.json").read_text())
    assert len(frames) == 2
    for frame in frames:
        assert frame["relative_path"].startswith("materials/frames/")
        assert (tmp_path / frame["relative_path"]).is_file()
    assert {p.name for p in tmp_path.iterdir()} == {"notes.md", "chapters-transcript.md", "materials"}


def test_interrupted_download_retains_task_identity_for_retry(tmp_path, monkeypatch):
    client = module.Client("http://127.0.0.1:18121")
    monkeypatch.setattr(client, "request", lambda method, path: (
        {"artifacts": [{"name": "transcript.json"}]} if path.endswith("/artifacts")
        else {"task_id": "A", "status": "completed"}))

    def interrupted(route, path):
        raise OSError("Download interrupted")

    monkeypatch.setattr(client, "download", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        client.fetch("A", tmp_path)
    bundle_path = tmp_path / "materials/bundle.json"
    assert not json.loads(bundle_path.read_text())["download_complete"]
    with pytest.raises(ValueError, match="其他任务"):
        client.fetch("B", tmp_path)
    monkeypatch.setattr(client, "download", lambda route, path: path.write_text("{}"))
    assert client.fetch("A", tmp_path)["download_complete"]


@pytest.mark.parametrize("case", ["local_version", "remote_version", "task_owner", "manifest_owner"])
def test_fetch_preflight_rejects_version_or_owner_before_write(tmp_path,monkeypatch,case):
    output=tmp_path/"new"
    if case=="local_version":
        materials=output/"materials"
        materials.mkdir(parents=True)
        (materials/"bundle.json").write_text('{"task_id":"A","schema_version":99}')
        (output/"notes.md").write_text("preserve reading")
        (output/"chapters-transcript.md").write_text("preserve original")
        (materials/"corrections.md").write_text("preserve evidence")
    before={str(p.relative_to(output)):p.read_bytes() for p in output.rglob("*") if p.is_file()} if output.exists() else {}
    client=module.Client("http://127.0.0.1:18121")
    def request(method,path):
        if case=="local_version":
            pytest.fail("Local preflight must precede network")
        if path.endswith("/artifacts"):
            return {"schema_version":99 if case=="remote_version" else 2,"task_id":"B" if case=="manifest_owner" else "A","artifacts":[]}
        return {"task_id":"B" if case=="task_owner" else "A","status":"completed"}
    monkeypatch.setattr(client,"request",request)
    monkeypatch.setattr(client,"download",lambda *a:pytest.fail("No download before preflight"))
    with pytest.raises(ValueError):
        client.fetch("A",output)
    if before:
        assert {str(p.relative_to(output)):p.read_bytes() for p in output.rglob("*") if p.is_file()}==before
    else:
        assert not output.exists()


@pytest.mark.parametrize("explicit,env,want",[(None,None,"http://127.0.0.1:18121"),(None,"http://environment:8","http://environment:8"),("http://explicit:9","http://environment:8","http://explicit:9")])
def test_cli_url_precedence(explicit,env,want,monkeypatch,capsys):
    monkeypatch.delenv("FRAMENOTE_SERVICE_URL",raising=False)
    if env:
        monkeypatch.setenv("FRAMENOTE_SERVICE_URL",env)
    args=["headless.py"]+(["--service-url",explicit] if explicit else [])+["status","A"]
    monkeypatch.setattr(sys,"argv",args)
    monkeypatch.setattr(module.Client,"request",lambda self,*a,**k:{"service_url":self.base})
    module.main()
    assert json.loads(capsys.readouterr().out)["service_url"]==want


def test_cli_copied_skill_paths(tmp_path):
    script=Path(module.__file__).resolve()
    env={k:v for k,v in os.environ.items() if k!="FRAMENOTE_NOTES_DIR"}
    def root(path):
        child=subprocess.run([sys.executable,"-S",str(path),"notes-dir"],cwd=tmp_path,env=env,capture_output=True,text=True)
        assert child.returncode==0,child.stderr
        return json.loads(child.stdout)["notes_directory"]
    assert root(script)==str(script.parents[3]/"workspace/notes")
    copied=tmp_path/"project/.agents/skills/framenote/scripts/headless.py"
    copied.parent.mkdir(parents=True)
    shutil.copyfile(script,copied)
    assert root(copied)==str(tmp_path/"framenote-notes")
    env["FRAMENOTE_NOTES_DIR"]="custom-notes"
    assert root(copied)==str(tmp_path/"custom-notes")


def test_standalone_script_in_short_path(tmp_path):
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".py",dir="/tmp") as file:
        shutil.copyfile(module.__file__,file.name)
        env={k:v for k,v in os.environ.items() if k!="FRAMENOTE_NOTES_DIR"}
        child=subprocess.run([sys.executable,"-S",file.name,"notes-dir"],cwd=tmp_path,env=env,capture_output=True,text=True)
        assert child.returncode==0,child.stderr
        assert json.loads(child.stdout)["notes_directory"]==str(tmp_path/"framenote-notes")
