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


@pytest.fixture(autouse=True)
def isolate_client_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "user_config_path", lambda: tmp_path / "user/client.json")
    for key in ("FRAMENOTE_SERVICE_URL", "FRAMENOTE_NOTES_DIR", "FRAMENOTE_DOWNLOAD_VIDEO"):
        monkeypatch.delenv(key, raising=False)


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
    (tmp_path / ".git").mkdir()
    script=Path(module.__file__).resolve()
    env={k:v for k,v in os.environ.items() if k!="FRAMENOTE_NOTES_DIR"}
    def root(path):
        child=subprocess.run([sys.executable,"-S",str(path),"notes-dir"],cwd=tmp_path,env=env,capture_output=True,text=True)
        assert child.returncode==0,child.stderr
        return json.loads(child.stdout)["notes_directory"]
    assert root(script)==str(tmp_path/"framenote-notes")
    copied=tmp_path/"project/.agents/skills/framenote/scripts/headless.py"
    copied.parent.mkdir(parents=True)
    shutil.copyfile(script,copied)
    assert root(copied)==str(tmp_path/"framenote-notes")
    env["FRAMENOTE_NOTES_DIR"]="custom-notes"
    assert root(copied)==str(tmp_path/"custom-notes")


def test_standalone_script_in_short_path(tmp_path):
    (tmp_path / ".git").mkdir()
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".py",dir="/tmp") as file:
        shutil.copyfile(module.__file__,file.name)
        env={k:v for k,v in os.environ.items() if k!="FRAMENOTE_NOTES_DIR"}
        child=subprocess.run([sys.executable,"-S",file.name,"notes-dir"],cwd=tmp_path,env=env,capture_output=True,text=True)
        assert child.returncode==0,child.stderr
        assert json.loads(child.stdout)["notes_directory"]==str(tmp_path/"framenote-notes")


def test_config_merges_fields_and_resolves_paths_at_their_config_file(tmp_path):
    user = tmp_path / "user/client.json"
    user.parent.mkdir()
    user.write_text(json.dumps({"service_url": "http://user:8", "notes_dir": "user-notes", "download_video": True}))
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / ".git").mkdir()
    config_path = project / ".framenote.json"
    config_path.write_text('{"service_url":"http://project:9"}')
    result = module.resolve_config(cwd=project / "docs", user_path=user, environ={})
    assert result["service_url"] == "http://project:9"
    assert result["download_video"] is True
    assert result["notes_dir"] == str(user.parent / "user-notes")
    assert result["project_config"] == str(config_path)
    config_path.write_text('{"notes_dir":"project-notes", "download_video":false}')
    result = module.resolve_config(cwd=project / "docs", user_path=user, environ={})
    assert result["service_url"] == "http://user:8"
    assert result["download_video"] is False
    assert result["notes_dir"] == str(project / "project-notes")
    result = module.resolve_config(cwd=project / "docs", user_path=user,
        environ={"FRAMENOTE_SERVICE_URL":"http://env:7", "FRAMENOTE_NOTES_DIR":"env-notes", "FRAMENOTE_DOWNLOAD_VIDEO":"true"},
        service_url="http://explicit:6", download_video=False)
    assert result["service_url"] == "http://explicit:6"
    assert result["download_video"] is False
    assert result["notes_dir"] == str(project / "docs/env-notes")


@pytest.mark.parametrize("git_file", [False, True])
def test_project_lookup_uses_nearest_file_and_stops_at_repository_boundary(tmp_path, git_file):
    (tmp_path / ".framenote.json").write_text('{"service_url":"http://outside:8"}')
    project = tmp_path / "project"
    child = project / "docs/sub"
    child.mkdir(parents=True)
    if git_file:
        (project / ".git").write_text("gitdir: /elsewhere")
    else:
        (project / ".git").mkdir()
    user = tmp_path / "user.json"
    assert module.resolve_config(cwd=child, user_path=user, environ={})["project_config"] is None
    root_config = project / ".framenote.json"
    root_config.write_text('{"service_url":"http://root:8"}')
    near_config = child.parent / ".framenote.json"
    near_config.write_text('{"service_url":"http://near:9"}')
    result = module.resolve_config(cwd=child, user_path=user, environ={})
    assert result["service_url"] == "http://near:9"
    assert result["project_config"] == str(near_config)


def test_config_outside_git_and_skill_install_location_are_independent(tmp_path):
    user = tmp_path / "user.json"
    for name, port in (("a", 8), ("b", 9)):
        root = tmp_path / name
        (root / "notes").mkdir(parents=True)
        (root / ".framenote.json").write_text(json.dumps({"service_url": f"http://server:{port}"}))
        result = module.resolve_config(cwd=root / "notes", user_path=user, environ={})
        assert result["service_url"] == f"http://server:{port}"
    result = module.resolve_config(cwd=tmp_path, user_path=user, environ={})
    assert result["service_url"] == "http://127.0.0.1:18121"
    assert result["download_video"] is False


@pytest.mark.parametrize("content", ['{"download_video":"false"}', '{"notes_dir":42}', '{"typo":true}', '[]', '{'])
def test_invalid_client_configuration_fails_explicitly(tmp_path, content):
    user = tmp_path / "user.json"
    user.write_text(content)
    with pytest.raises(ValueError):
        module.resolve_config(cwd=tmp_path, user_path=user, environ={})


@pytest.mark.parametrize("value,want", [("true",True), ("1",True), ("yes",True), ("false",False), ("0",False), ("off",False)])
def test_environment_video_boolean_is_parsed(tmp_path, value, want):
    result = module.resolve_config(cwd=tmp_path, user_path=tmp_path / "missing", environ={"FRAMENOTE_DOWNLOAD_VIDEO":value})
    assert result["download_video"] is want


@pytest.mark.parametrize("before", [True, False])
@pytest.mark.parametrize("flag,want", [("--download-video",True), ("--no-download-video",False)])
def test_cli_video_flag_overrides_project_default_on_either_side_of_command(tmp_path, monkeypatch, capsys, before, flag, want):
    (tmp_path / ".framenote.json").write_text(json.dumps({"download_video":not want}))
    args = ["headless.py"] + ([flag] if before else []) + ["fetch", "A", "--out-dir", str(tmp_path / "note")] + ([] if before else [flag])
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(module.Client, "fetch", lambda self, task_id, output, **kwargs: kwargs)
    module.main()
    assert json.loads(capsys.readouterr().out)["download_video"] is want


def test_cli_task_listing_and_transcript_range_use_http_api(monkeypatch, capsys):
    monkeypatch.setattr(module.Client, "request", lambda self, method, path, **kwargs: {"method":method,"path":path})
    monkeypatch.setattr(sys, "argv", ["headless.py", "tasks", "--offset", "2", "--limit", "3"])
    module.main()
    assert json.loads(capsys.readouterr().out)["path"] == "/api/tasks?offset=2&limit=3"
    monkeypatch.setattr(sys, "argv", ["headless.py", "transcript", "A", "--start", "30", "--end", "60", "--offset", "2", "--limit", "3"])
    module.main()
    assert json.loads(capsys.readouterr().out)["path"] == "/api/task/A/transcript?start=30.0&offset=2&limit=3&output_format=json&end=60.0"


def test_fetch_rejects_another_service_before_writes(tmp_path, monkeypatch):
    materials = tmp_path / "materials"
    materials.mkdir()
    marker = materials / "bundle.json"
    marker.write_text('{"task_id":"A","service_url":"http://other:8"}')
    client = module.Client("http://server:9")
    monkeypatch.setattr(client, "request", lambda *args: pytest.fail("No network before ownership check"))
    with pytest.raises(ValueError, match="其他服务"):
        client.fetch("A", tmp_path)
    assert json.loads(marker.read_text())["service_url"] == "http://other:8"


@pytest.fixture
def material_server():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    state = {"requests": [], "video": b"retained-video" * 100000, "truncate": False, "filename": "video.mp4"}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            state["requests"].append((self.path, self.headers.get("If-None-Match")))
            if self.path.endswith("/video"):
                if self.headers.get("If-None-Match") == '"v1"':
                    self.send_response(304)
                    self.end_headers()
                    return
                body = state["video"]
                self.send_response(200)
                self.send_header("Content-Disposition", 'attachment; filename="' + state["filename"] + '"')
                self.send_header("Content-Length", str(len(body) + (5 if state["truncate"] else 0)))
                self.send_header("ETag", '"v1"')
            else:
                value = ({"artifacts":[{"name":"transcript.json"}]} if self.path.endswith("/artifacts")
                         else {"segments":[]} if self.path.endswith("transcript.json")
                         else {"task_id":"A","status":"completed","assets":{"video":"preview.mp4"},"downloads":{"video":"/api/task/A/video"}})
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield module.Client(f"http://127.0.0.1:{server.server_port}"), state
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_http_video_download_is_opt_in_and_reuses_only_verified_local_file(tmp_path, material_server):
    client, state = material_server
    client.fetch("A", tmp_path)
    assert not any(path.endswith("/video") for path, _ in state["requests"])
    (tmp_path / "notes.md").write_text("Keep my notes")
    first = client.fetch("A", tmp_path, download_video=True)
    video = tmp_path / first["video"]["relative_path"]
    assert video.read_bytes() == state["video"]
    modified = video.stat().st_mtime_ns
    client.fetch("A", tmp_path, download_video=True)
    assert state["requests"][-1] == ("/api/task/A/video", '"v1"')
    assert video.stat().st_mtime_ns == modified
    video.write_bytes(b"changed")
    client.fetch("A", tmp_path, download_video=True)
    assert state["requests"][-1] == ("/api/task/A/video", None)
    assert video.read_bytes() == state["video"]
    assert client.fetch("A", tmp_path)["video"]["sha256"] == first["video"]["sha256"]
    assert video.is_file()
    assert (tmp_path / "notes.md").read_text() == "Keep my notes"


def test_http_partial_video_is_cleaned_and_explicit_request_remains_incomplete(tmp_path, material_server):
    client, state = material_server
    state["truncate"] = True
    with pytest.raises((OSError, module.http.client.IncompleteRead)):
        client.fetch("A", tmp_path, download_video=True)
    assert not (tmp_path / "materials/video.mp4").exists()
    assert not list(tmp_path.rglob("*.part"))
    assert not json.loads((tmp_path / "materials/bundle.json").read_text())["download_complete"]
    state["truncate"] = False
    assert client.fetch("A", tmp_path, download_video=True)["download_complete"]


def test_http_video_filename_cannot_overwrite_reading_document(tmp_path, material_server):
    client, state = material_server
    state["filename"] = "../../notes.md"
    (tmp_path / "notes.md").write_text("Keep my notes")
    with pytest.raises(ValueError, match="filename"):
        client.fetch("A", tmp_path, download_video=True)
    assert (tmp_path / "notes.md").read_text() == "Keep my notes"


def test_service_workflow_with_standalone_cli_and_project_configuration(tmp_path, video):
    import socket
    import threading
    import time
    import uvicorn
    from framenote.service import create_app
    from framenote.service_settings import Settings
    from framenote.task_store import atomic_json

    app = create_app(Settings.from_env({}, base_dir=tmp_path / "backend"))
    store = app.state.store
    task = store.create(status="completed", phase="completed", duration=5, assets={"video":"source.mp4"})
    task_id = task["task_id"]
    directory = store.directory(task_id)
    shutil.copyfile(video, directory / "source.mp4")
    atomic_json(directory / "transcript.json", {"timestamp_kind":"VAD_interval", "duration":5,
        "segments":[{"start":i,"end":i+1,"text":f"段落{i}"} for i in range(5)]})
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    worker = threading.Thread(target=server.run, kwargs={"sockets":[listener]}, daemon=True)
    worker.start()
    project = tmp_path / "agent-project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".framenote.json").write_text(json.dumps({"service_url":base,"download_video":False}))
    note = project / "my-note"
    environment = {key:value for key,value in os.environ.items() if not key.startswith("FRAMENOTE_")}
    def run(*args):
        child = subprocess.run([sys.executable,"-S",module.__file__,*args], cwd=project, env=environment,
                               capture_output=True,text=True,timeout=30)
        assert child.returncode == 0, child.stderr
        return json.loads(child.stdout)
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(.01)
        assert server.started
        assert run("config")["service_url"] == base
        assert run("health")["status"] == "ok"
        assert run("tasks")["tasks"][0]["task_id"] == task_id
        assert run("status",task_id)["downloads"]["video"] == f"/api/task/{task_id}/video"
        assert run("transcript",task_id,"--start","1.5","--end","4","--limit","1","--offset","1")["text"] == "段落2"
        assert run("fetch",task_id,"--out-dir",str(note))["download_complete"]
        assert not (note / "materials/video.mp4").exists()
        (note / "notes.md").write_text("My reading notes")
        result = run("frames",task_id,"--timestamps","1.2","3","--out-dir",str(note))
        assert result["download_complete"]
        frames = json.loads((note / "materials/frames.json").read_text())
        assert len(frames) == 2
        assert all((note / item["relative_path"]).read_bytes().startswith(b"\xff\xd8") for item in frames)
        result = run("fetch",task_id,"--out-dir",str(note),"--download-video")
        assert result["video"]["sha256"] == module.digest(video)
        assert (note / "materials/video.mp4").read_bytes() == video.read_bytes()
        stamp = (note / "materials/video.mp4").stat().st_mtime_ns
        run("fetch",task_id,"--out-dir",str(note),"--download-video")
        assert (note / "materials/video.mp4").stat().st_mtime_ns == stamp
        assert (note / "notes.md").read_text() == "My reading notes"
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()
        assert not worker.is_alive()
