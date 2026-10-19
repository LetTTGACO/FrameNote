import importlib.util
import json
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


def test_refetch_adds_frames_under_materials_and_preserves_reading_documents(tmp_path, monkeypatch):
    client = module.Client("http://127.0.0.1:18121")
    frame_ids = ["a" * 32]
    notes = "# Notes\n\n![frame](materials/frames/" + frame_ids[0] + ".jpg)\n"
    (tmp_path / "notes.md").write_text(notes)
    (tmp_path / "chapters-transcript.md").write_text("Original chapter transcript")

    def request(method, path):
        if path.endswith("/artifacts"):
            return {"artifacts": [{"name": "frames.json"}, {"name": "transcript.md"}]}
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
