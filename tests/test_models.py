import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from framenote.models import prepare_models, verify_models, required_assets


@pytest.fixture
def source(tmp_path):
    state = {"requests": 0, "failure": None}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"] += 1
            content = b"sense-data" if self.path == "/sense" else b"vad-data"
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content[:2] if state["failure"] == "disconnect" else content)
            self.close_connection = True
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    files = []
    for group, route, content in [("sensevoice", "sense", b"sense-data"), ("vad", "vad", b"vad-data")]:
        files.append({"target": group+"/model_quant.onnx", "repo": "test/source", "revision": "a"*40,
                      "url": f"http://127.0.0.1:{server.server_port}/{route}", "bytes": len(content),
                      "sha256": hashlib.sha256(content).hexdigest(), "license_url": "https://example.test/license"})
    manifest = tmp_path/"lock.json"
    manifest.write_text(json.dumps({"schema_version": 1, "files": files}))
    try:
        yield manifest, state
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_prepare_is_atomic_and_reuses_valid_assets(source, tmp_path):
    manifest, state = source
    output = tmp_path/"models"
    prepare_models(output, manifest=manifest)
    assert (output/"sensevoice/model_quant.onnx").read_bytes() == b"sense-data"
    assert (output/"vad/model_quant.onnx").read_bytes() == b"vad-data"
    requests = state["requests"]
    prepare_models(output, manifest=manifest)
    assert state["requests"] == requests == 2
    assert not list(output.rglob("*.part"))
    verify_models(output, manifest=manifest)


@pytest.mark.parametrize("failure", ["disconnect", "hash"])
def test_download_failure_preserves_existing_files(source, tmp_path, failure):
    manifest, state = source
    output = tmp_path/"models"
    previous = output/"sensevoice/model_quant.onnx"
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"sense-data")
    if failure == "disconnect":
        state["failure"] = failure
    else:
        lock = json.loads(manifest.read_text())
        lock["files"][1]["sha256"] = "0"*64
        manifest.write_text(json.dumps(lock))
    with pytest.raises((OSError, ValueError)):
        prepare_models(output, manifest=manifest)
    assert previous.read_bytes() == b"sense-data"
    assert not (output/"vad/model_quant.onnx").exists()
    assert not list(output.rglob("*.part"))


def test_verify_is_offline_and_names_missing_or_corrupt_assets(source, tmp_path, monkeypatch):
    manifest, _ = source
    output = tmp_path/"models"
    prepare_models(output, manifest=manifest)
    (output/"sensevoice/model_quant.onnx").unlink()
    (output/"vad/model_quant.onnx").write_bytes(b"corrupt")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("verify must be offline"))
    with pytest.raises(ValueError) as exc:
        verify_models(output, manifest=manifest)
    assert "sensevoice/model_quant.onnx" in str(exc.value)
    assert "vad/model_quant.onnx" in str(exc.value)
    child = subprocess.run([sys.executable, "-m", "framenote.models", "verify", "--out", str(output), "--manifest", str(manifest)], capture_output=True, text=True)
    assert child.returncode == 1
    assert "sensevoice/model_quant.onnx" in child.stderr


@pytest.mark.parametrize("case", ["parent", "absolute", "duplicate", "version", "symlink"])
def test_manifest_rejects_unsafe_targets_before_writing(source, tmp_path, case):
    manifest, state = source
    lock = json.loads(manifest.read_text())
    output = tmp_path/"models"
    outside = tmp_path/"outside"
    outside.mkdir()
    keep = outside/"keep"
    keep.write_text("preserve")
    if case == "parent":
        lock["files"][1]["target"] = "../outside/keep"
    elif case == "absolute":
        lock["files"][1]["target"] = str(keep)
    elif case == "duplicate":
        lock["files"][1]["target"] = lock["files"][0]["target"]
    elif case == "version":
        lock["schema_version"] = 99
    else:
        output.mkdir()
        (output/"vad").symlink_to(outside, target_is_directory=True)
    manifest.write_text(json.dumps(lock))
    with pytest.raises(ValueError):
        prepare_models(output, manifest=manifest)
    assert keep.read_text() == "preserve"
    assert state["requests"] == 0
    assert not (output/"sensevoice/model_quant.onnx").exists()


def test_default_lock_is_packaged_resource(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assets = required_assets()
    assert set(assets) == {"sensevoice", "vad"}
    assert len(assets["sensevoice"]) == 6
    assert len(assets["vad"]) == 4
    assert "chn_jpn_yue_eng_ko_spectok.bpe.model" in assets["sensevoice"]
