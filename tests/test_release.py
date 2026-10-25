import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

from framenote import __version__
from framenote import release


@pytest.fixture
def release_inputs(tmp_path, monkeypatch):
    identity = {"version": __version__, "commit": "a" * 40, "dirty": False}
    wheel = tmp_path / f"framenote-{__version__}-py3-none-any.whl"
    source = tmp_path / f"framenote-{__version__}.tar.gz"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("framenote/_build_info.json", json.dumps(identity))
    with tarfile.open(source, "w:gz") as archive:
        data = json.dumps(identity).encode()
        item = tarfile.TarInfo(f"framenote-{__version__}/framenote/_build_info.json")
        item.size = len(data)
        archive.addfile(item, io.BytesIO(data))
    monkeypatch.setattr(release, "build_info", lambda: dict(identity))
    monkeypatch.setattr(release, "environment_snapshot", lambda: {
        "python": "3.12.13", "system": "fixture", "packages": {"framenote": __version__, "pip": "25.0"}})
    return wheel, source, tmp_path / "bundle", identity


def test_archive_verifies_exact_bytes_and_refuses_overwrite(release_inputs):
    wheel, source, output, identity = release_inputs
    manifest = release.archive_release(wheel, source, output)
    assert release.verify_release(output) == manifest
    assert manifest["commit"] == identity["commit"]
    assert manifest["material_schemas"] == {"read": [1, 2, 3], "write": [2, 3]}
    assert (output / "requirements.lock").read_text() == f"framenote=={__version__}\npip==25.0\n"
    original = {p.name: p.read_bytes() for p in output.iterdir()}
    with pytest.raises(FileExistsError):
        release.archive_release(wheel, source, output)
    assert {p.name: p.read_bytes() for p in output.iterdir()} == original


@pytest.mark.parametrize("change", ["wheel", "manifest", "extra", "symlink"])
def test_verify_rejects_tampering_and_unexpected_files(release_inputs, change):
    wheel, source, output, _ = release_inputs
    release.archive_release(wheel, source, output)
    if change == "wheel":
        (output / wheel.name).write_bytes(b"changed")
    elif change == "manifest":
        data = json.loads((output / "release.json").read_text())
        data["commit"] = "b" * 40
        (output / "release.json").write_text(json.dumps(data))
    elif change == "extra":
        (output / "secret.txt").write_text("unexpected")
    else:
        (output / "models.lock.json").unlink()
        (output / "models.lock.json").symlink_to(source)
    with pytest.raises(ValueError):
        release.verify_release(output)


@pytest.mark.parametrize("dirty,commit", [(True, "a" * 40), (None, None)])
def test_archive_rejects_unknown_or_uncommitted_source(release_inputs, monkeypatch, dirty, commit):
    wheel, source, output, identity = release_inputs
    monkeypatch.setattr(release, "build_info", lambda: {**identity, "dirty": dirty, "commit": commit})
    with pytest.raises(ValueError, match="clean, known"):
        release.archive_release(wheel, source, output)
    assert not output.exists()


def test_archive_rejects_mismatched_source_identity(release_inputs):
    wheel, source, output, identity = release_inputs
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("framenote/_build_info.json", json.dumps({**identity, "commit": "b" * 40}))
    with pytest.raises(ValueError, match="Wheel identity"):
        release.archive_release(wheel, source, output)
    assert not output.exists()


@pytest.mark.parametrize("direct_url", [
    {"url": "file:///private/token/wheel.whl", "archive_info": {}},
    {"url": "file:///private/token/checkout", "dir_info": {"editable": True}},
])
def test_environment_inventory_omits_urls_and_rejects_editable_dependencies(monkeypatch, direct_url):
    package = SimpleNamespace(metadata={"Name": "FrameNote"}, version=__version__,
                              read_text=lambda name: json.dumps(direct_url))
    monkeypatch.setattr(release.metadata, "distributions", lambda: [package])
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: None)
    if "dir_info" in direct_url:
        with pytest.raises(ValueError, match="built distributions"):
            release.environment_snapshot()
    else:
        snapshot = release.environment_snapshot()
        assert snapshot["packages"] == {"framenote": __version__}
        assert "private" not in json.dumps(snapshot) and "token" not in json.dumps(snapshot)
