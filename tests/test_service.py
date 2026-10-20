from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from framenote.asr_sensevoice import split_intervals
from framenote.frame_extractor import extract_frame
from framenote.galaxy_downloader import GalaxyDownloader
from framenote.service import Jobs, create_app
from framenote.service_pipeline import Pipeline, platform_url
from framenote.service_process import run_process
from framenote.service_settings import Settings, SenseVoiceConfig
from framenote.task_store import TaskStore, atomic_json


def config(tmp_path, **kwargs):
    return Settings(workspace=tmp_path, sensevoice=SenseVoiceConfig(tmp_path / "sense", tmp_path / "vad", sys.executable), **kwargs)


def client_for(settings):
    return TestClient(create_app(settings), client=("192.168.31.42", 51000))




def finished_task(store, video=None):
    task = store.create(status="completed", phase="completed", duration=5,
                        assets={"video": "source.mp4"} if video else {})
    directory = store.directory(task["task_id"])
    if video:
        (directory / "source.mp4").write_bytes(video.read_bytes())
    atomic_json(directory / "transcript.json", {"timestamp_kind": "VAD_interval", "duration": 5,
                "segments": [{"start": i, "end": i + 1, "text": f"段落{i}"} for i in range(5)]})
    return task["task_id"]


def test_persistence_recovery_does_not_lose_completed_or_queued_tasks(tmp_path):
    store = TaskStore(tmp_path)
    done = finished_task(store)
    queued = store.create(status="queued")["task_id"]
    interrupted = store.create(status="processing")["task_id"]
    recovered = TaskStore(tmp_path)
    assert recovered.recover() == [queued]
    assert recovered.get(done)["status"] == "completed"
    assert recovered.get(interrupted)["error"]["code"] == "interrupted"


@pytest.mark.parametrize("url", ["http://127.0.0.1/a", "https://douyin.com.evil.test/a", "file:///etc/passwd",
                                    "https://example.com/video", "https://user@www.douyin.com/video/123"])
def test_input_is_limited_to_supported_platforms(url):
    with pytest.raises(ValueError):
        platform_url(url)


def test_bilibili_page_identity_is_preserved():
    assert "p=1" in platform_url("https://www.bilibili.com/video/BV1cfaf6SEA9/")
    assert "p=2" in platform_url("https://www.bilibili.com/video/BV1cfaf6SEA9/?p=2")


def test_transcript_range_pagination_and_restart(tmp_path):
    settings = config(tmp_path)
    with client_for(settings) as client:
        task_id = finished_task(client.app.state.store)
        response = client.get(f"/api/task/{task_id}/transcript", params={"start": 1.5, "end": 4, "limit": 1, "offset": 1})
        assert response.status_code == 200
        assert response.json()["total_segments"] == 3
        assert response.json()["segments"][0]["text"] == "段落2"
        assert response.json()["timestamp_kind"] == "VAD_interval"
        assert client.get(f"/api/task/{task_id}/transcript?start=4&end=2").status_code == 422
    with client_for(settings) as client:
        assert client.get(f"/api/task/{task_id}").json()["status"] == "completed"
        assert client.get("/api/tasks").json()["total"] == 1


def test_upload_preserves_video_and_bounds_size(tmp_path, video):
    settings = config(tmp_path / "workspace", max_upload_bytes=video.stat().st_size)
    with client_for(settings) as client:
        response = client.post("/api/upload", files={"file": ("../教学.mp4", video.read_bytes(), "video/mp4")})
        task_id = response.json()["upload_task_id"]
        task = client.get(f"/api/task/{task_id}").json()
        assert task["status"] == "uploaded"
        assert (client.app.state.store.directory(task_id) / "source.mp4").read_bytes() == video.read_bytes()
        assert client.post("/api/upload", files={"file": ("big.mp4", b"x" * (video.stat().st_size + 1))}).status_code == 413
        assert client.post("/api/upload", files={"file": ("x.py", b"print(1)")}).status_code == 422


def test_independent_frames_decode_past_keyframe_and_survive_restart(tmp_path, video):
    settings = config(tmp_path / "workspace")
    with client_for(settings) as client:
        task_id = finished_task(client.app.state.store, video)
        response = client.post(f"/api/task/{task_id}/frames", json={"timestamps": [1.2, 3]})
        assert response.status_code == 200
        frames = response.json()["frames"]
        assert abs(frames[0]["actual_time"] - 1.2) < .041
        assert abs(frames[1]["actual_time"] - 3) < .041
        assert frames[0]["sha256"] != frames[1]["sha256"]
        assert client.get(frames[0]["url"]).headers["content-type"] == "image/jpeg"
        assert client.post(f"/api/task/{task_id}/frames", json={"timestamps": [5]}).status_code == 422
        assert client.get(f"/api/task/{task_id}/artifacts/task.json").status_code == 404
    with client_for(settings) as client:
        assert client.get(frames[0]["url"]).status_code == 200


def test_no_ui_and_lan_access(tmp_path):
    app = create_app(config(tmp_path))
    with TestClient(app, client=("192.168.31.42", 50000)) as client:
        assert client.get("/").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.post("/mcp", json={"method":"tools/list"}).status_code == 404
        assert client.get("/api/health").json()["mode"] == "headless"
        assert client.post("/api/transcribe", json={"video_url": "https://example.com"}).status_code == 422
        assert client.get("/api/health").status_code == 200


def test_retained_video_download_is_discoverable_and_does_not_mutate_task(tmp_path, video):
    with client_for(config(tmp_path / "workspace")) as client:
        task_id = finished_task(client.app.state.store, video)
        directory = client.app.state.store.directory(task_id)
        before = (directory / "task.json").read_bytes()
        route = f"/api/task/{task_id}/video"
        assert client.get(f"/api/task/{task_id}").json()["downloads"]["video"] == route
        manifest = client.get(f"/api/task/{task_id}/artifacts").json()
        assert manifest["downloads"]["video"] == route
        assert all(item["name"] != "source.mp4" for item in manifest["artifacts"])
        response = client.get(route)
        assert response.status_code == 200
        assert response.content == video.read_bytes()
        assert "video.mp4" in response.headers["content-disposition"]
        assert client.get(route, headers={"If-None-Match": response.headers["etag"]}).status_code == 304
        assert (directory / "task.json").read_bytes() == before
        assert client.get("/api/tasks").json()["tasks"][0]["downloads"]["video"] == route


@pytest.mark.parametrize("name", [None, "../outside.mp4", "/tmp/outside.mp4", "escape.mp4"])
def test_video_download_rejects_missing_and_external_assets(tmp_path, name):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"private")
    with client_for(config(tmp_path / "workspace")) as client:
        task = client.app.state.store.create(status="completed", assets={"video": name} if name else {})
        task_id = task["task_id"]
        if name == "escape.mp4":
            (client.app.state.store.directory(task_id) / name).symlink_to(outside)
        assert client.get(f"/api/task/{task_id}").json()["downloads"] == {}
        assert client.get(f"/api/task/{task_id}/video").status_code == 409


def test_separate_retained_audio_is_muxed_once_without_changing_sources(tmp_path, video):
    with client_for(config(tmp_path / "workspace")) as client:
        task_id = finished_task(client.app.state.store)
        directory = client.app.state.store.directory(task_id)
        source, audio = directory / "preview.mp4", directory / "original.m4a"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-an", "-c:v", "copy", str(source)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-c:a", "copy", str(audio)], check=True)
        original = {path.name: path.read_bytes() for path in (source, audio)}
        atomic_json(directory / "acquisition.json", {"video": source.name, "audio": audio.name})
        client.app.state.store.update(task_id, assets={"video": source.name, "audio": "audio.flac"})
        response = client.get(f"/api/task/{task_id}/video")
        assert response.status_code == 200
        downloaded = tmp_path / "downloaded.mp4"
        downloaded.write_bytes(response.content)
        media = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(downloaded)],
            capture_output=True, text=True, check=True).stdout)
        assert {stream["codec_type"] for stream in media["streams"]} == {"video", "audio"}
        cached = directory / "video.download.mp4"
        before = cached.stat().st_mtime_ns
        assert client.get(f"/api/task/{task_id}/video", headers={"If-None-Match": response.headers["etag"]}).status_code == 304
        assert cached.stat().st_mtime_ns == before
        assert {path.name: path.read_bytes() for path in (source, audio)} == original
        assert not list(directory.glob("video.download.*.mp4"))


@pytest.mark.asyncio
async def test_cancel_reaps_actual_subprocess(tmp_path):
    marker = tmp_path / "pid"
    task = asyncio.create_task(run_process([sys.executable, "-c", "import os,time,pathlib;pathlib.Path(__import__('sys').argv[1]).write_text(str(os.getpid()));time.sleep(30)", str(marker)]))
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(.02)
    assert marker.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    import os
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


@pytest.mark.asyncio
async def test_galaxy_contract_rejects_failure_and_commits_only_complete_download(tmp_path, monkeypatch):
    calls = []

    async def fake_run(args, **kwargs):
        calls.append(args)
        if "/api/parse?" in args[-1]:
            Path(args[args.index("--output") + 1]).write_text(json.dumps({"success": True, "data": {"platform": "douyin", "title": "教程", "downloadVideoUrl": "/api/download?id=secret"}}))
            return b""
        Path(args[args.index("--output") + 1]).write_bytes(b"video")
        return b""

    monkeypatch.setattr("framenote.galaxy_downloader.run_process", fake_run)
    video_path, meta = await GalaxyDownloader("https://api.example").download("https://v.douyin.com/test", tmp_path)
    assert video_path.read_bytes() == b"video"
    assert calls[0][-1].startswith("https://api.example/api/parse?url=https%3A")
    assert meta["acquisition_provider"] == "galaxy_api" and "secret" not in json.dumps(meta)
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.private.json"))


@pytest.mark.asyncio
async def test_galaxy_only_runs_as_configured_douyin_fallback(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks")
    pipe = Pipeline(config(tmp_path, galaxy_enabled=True), store)
    called = []

    async def failing_info(*args, **kwargs):
        raise RuntimeError("403")

    async def fake_galaxy(self, url, directory):
        called.append(url)
        return directory / "source.mp4", {"acquisition_provider": "galaxy_api"}

    monkeypatch.setattr(pipe, "acquire_upstream", failing_info)
    monkeypatch.setattr(GalaxyDownloader, "download", fake_galaxy)
    douyin = store.create(video_url="https://v.douyin.com/test")["task_id"]
    assert (await pipe.acquire(douyin, lambda: False))[2]["acquisition_provider"] == "galaxy_api"
    bili = store.create(video_url="https://www.bilibili.com/video/BV1cfaf6SEA9/")["task_id"]
    with pytest.raises(RuntimeError):
        await pipe.acquire(bili, lambda: False)
    assert len(called) == 1
    off = Pipeline(config(tmp_path), store)
    monkeypatch.setattr(off, "acquire_upstream", failing_info)
    with pytest.raises(RuntimeError):
        await off.acquire(douyin, lambda: False)


def test_vad_offset_and_maximum_segment_length():
    assert split_intervals([[0, 65000]], 300, 370) == [(300, 330), (330, 360), (360, 365)]
    assert split_intervals([[0, 5000]], 300, 302) == [(300, 302)]


@pytest.mark.asyncio
async def test_jobs_serialize_cancel_and_continue_queue(tmp_path):
    store = TaskStore(tmp_path)
    first, skipped, last = [store.create()["task_id"] for _ in range(3)]
    entered = asyncio.Event()
    ran = []

    class Runner:
        async def run(self, task_id, aborted):
            ran.append(task_id)
            if task_id == first:
                entered.set()
                await asyncio.sleep(30)
            store.update(task_id, status="completed")

    jobs = Jobs(store, Runner())
    await jobs.start()
    try:
        for task_id in (first, skipped, last):
            jobs.enqueue(task_id)
        await asyncio.wait_for(entered.wait(), 2)
        assert ran == [first]
        jobs.cancel(skipped)
        jobs.cancel(first)
        await asyncio.wait_for(jobs.queue.join(), 2)
        assert ran == [first, last]
        assert store.get(first)["status"] == store.get(skipped)["status"] == "cancelled"
        assert store.get(last)["status"] == "completed"
    finally:
        await jobs.stop()


@pytest.mark.asyncio
async def test_cancel_acquisition_stops_blocking_worker_thread(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks")
    task_id = store.create()["task_id"]
    directory = store.directory(task_id)
    executable = tmp_path / "python-worker"
    executable.write_text(f"#!{sys.executable}\n" + '''import pathlib,sys,threading,time
root=pathlib.Path(sys.argv[sys.argv.index('--root')+1])
task_id=sys.argv[sys.argv.index('--task-id')+1]
def blocked_download():
    while True:
        (root/task_id/'heartbeat').write_text(str(time.monotonic()))
        time.sleep(.02)
threading.Thread(target=blocked_download).start()
''')
    executable.chmod(0o700)
    monkeypatch.setattr("framenote.service_pipeline.sys", SimpleNamespace(executable=str(executable)))
    pipe = Pipeline(config(tmp_path), store)
    running = asyncio.create_task(pipe.acquire_upstream(task_id, "https://v.douyin.com/test"))
    for _ in range(500):
        if (directory / "heartbeat").exists():
            break
        if running.done():
            await running
        await asyncio.sleep(.02)
    assert (directory / "heartbeat").exists()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    stopped = (directory / "heartbeat").read_text()
    await asyncio.sleep(.1)
    assert (directory / "heartbeat").read_text() == stopped


@pytest.mark.asyncio
async def test_cancel_reaps_descendant_that_ignores_term_after_leader_exits(tmp_path):
    import os
    import signal
    pid = tmp_path / "descendant.pid"
    heartbeat = tmp_path / "heartbeat"
    child = "import pathlib,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready',flush=True);p=pathlib.Path(sys.argv[1]);exec(\"while True:\\n p.write_text(str(time.monotonic()));time.sleep(.02)\")"
    parent = "import pathlib,subprocess,sys,time;child=subprocess.Popen([sys.executable,'-u','-c',sys.argv[1],sys.argv[3]],stdout=subprocess.PIPE);child.stdout.readline();pathlib.Path(sys.argv[2]).write_text(str(child.pid));time.sleep(60)"
    running = asyncio.create_task(run_process([sys.executable, "-u", "-c", parent, child, str(pid), str(heartbeat)]))
    try:
        for _ in range(300):
            if pid.exists() and heartbeat.exists():
                break
            if running.done():
                await running
            await asyncio.sleep(.01)
        assert pid.exists() and heartbeat.exists()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        previous = heartbeat.read_text()
        await asyncio.sleep(.1)
        assert heartbeat.read_text() == previous
    finally:
        if not running.done():
            running.cancel()
            with __import__('contextlib').suppress(asyncio.CancelledError):
                await running
        if pid.exists():
            with __import__('contextlib').suppress(ProcessLookupError):
                os.kill(int(pid.read_text()), signal.SIGKILL)


@pytest.mark.parametrize("output_format",["json","markdown","srt"])
def test_transcript_legacy_and_invalid_versions_preserve_files(tmp_path,output_format):
    with client_for(config(tmp_path)) as client:
        task_id=finished_task(client.app.state.store)
        path=client.app.state.store.directory(task_id)/"transcript.json"
        original=path.read_bytes()
        assert client.get(f"/api/task/{task_id}/transcript",params={"output_format":output_format}).status_code==200
        assert path.read_bytes()==original
        for payload,code in [({"schema_version":99},"unsupported_material_version"),({"duration":5,"segments":[{"text":"missing"}]},"invalid_transcript")]:
            path.write_text(json.dumps(payload))
            before=path.read_bytes()
            response=client.get(f"/api/task/{task_id}/transcript",params={"output_format":output_format})
            assert response.status_code==409
            assert response.json()["detail"]["code"]==code
            assert path.read_bytes()==before


def test_transcript_paging_formats_share_segments(tmp_path):
    with client_for(config(tmp_path)) as client:
        task_id=finished_task(client.app.state.store)
        pages=[]
        for output_format in ["json","markdown","srt"]:
            response=client.get(f"/api/task/{task_id}/transcript",params={"offset":1,"limit":2,"output_format":output_format})
            assert response.status_code==200
            payload=response.json()
            assert [s["text"] for s in payload["segments"]]==["段落1","段落2"]
            pages.append(payload["segments"])
            if output_format!="json":
                assert "段落1" in payload["content"] and "段落3" not in payload["content"]
        assert pages[0]==pages[1]==pages[2]
        assert client.get(f"/api/task/{task_id}/transcript?limit=501").status_code==422
        assert client.get(f"/api/task/{task_id}/transcript?start=-1").status_code==422
        assert client.get(f"/api/task/{task_id}/transcript?start=4&end=2").status_code==422


@pytest.mark.parametrize("output_format", ["json", "markdown", "srt"])
@pytest.mark.parametrize("selection,expected", [
    ({"offset": 1, "limit": 1}, ["段落1"]),
    ({"start": 1, "end": 2}, ["段落1"]),
    ({"offset": 5}, []),
])
def test_transcript_response_text_matches_selected_page(tmp_path, output_format, selection, expected):
    with client_for(config(tmp_path)) as client:
        task_id = finished_task(client.app.state.store)
        path = client.app.state.store.directory(task_id) / "transcript.json"
        original = path.read_bytes()
        response = client.get(f"/api/task/{task_id}/transcript", params={**selection, "output_format": output_format})
        assert response.status_code == 200
        page = response.json()
        assert [segment["text"] for segment in page["segments"]] == expected
        assert page["text"] == "\n".join(expected)
        if output_format != "json":
            assert all(text not in page["content"] for text in ["段落0", "段落2", "段落3", "段落4"])
        assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_stop_during_cancellation_waits_for_term_resistant_child(tmp_path):
    import os
    import signal
    from contextlib import suppress

    store = TaskStore(tmp_path / "tasks")
    task_id = store.create()["task_id"]
    ready = asyncio.Event()
    term_received = tmp_path / "term-received"
    child_pid = None

    def progress(line):
        nonlocal child_pid
        child_pid = int(line)
        ready.set()

    child_code = (
        "import os,pathlib,signal,sys,time; "
        "signal.signal(signal.SIGTERM, lambda *args: pathlib.Path(sys.argv[1]).write_text('term')); "
        "print(os.getpid(),flush=True); time.sleep(60)"
    )

    class Runner:
        async def run(self, task_id, aborted):
            await run_process([sys.executable, "-u", "-c", child_code, str(term_received)], progress=progress)

    jobs = Jobs(store, Runner())
    await jobs.start()
    try:
        jobs.enqueue(task_id)
        await asyncio.wait_for(ready.wait(), 5)
        jobs.cancel(task_id)
        for _ in range(200):
            if term_received.exists():
                break
            await asyncio.sleep(.01)
        assert term_received.exists(), "First cancellation must enter TERM cleanup before stop"
        await asyncio.wait_for(jobs.stop(), 10)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if child_pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(child_pid, signal.SIGKILL)
        await jobs.stop()
