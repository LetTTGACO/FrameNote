"""Exercise the MCP protocol and existing material APIs without network or ASR."""
import asyncio
import base64
import json
import time

from fastapi.testclient import TestClient
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest
import sys

from framenote.service import create_app
from framenote.service_mcp import MaterialTools, create_mcp
from framenote.service_pipeline import Pipeline
from framenote.service_settings import Settings
from framenote.task_store import atomic_json

TOOLS = {"transcribe_video", "get_task_status", "wait_for_task", "list_tasks", "get_transcript",
         "extract_frames", "get_frame", "get_artifacts", "cancel_task"}


def rpc(client, method, params=None):
    response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                           json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def call(client, name, **arguments):
    return rpc(client, "tools/call", {"name": name, "arguments": arguments})


def payload(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_http_material_workflow_and_restart(tmp_path, video, monkeypatch):
    monkeypatch.setenv("FRAMENOTE_SERVICE_URL", "http://127.0.0.1:1")
    settings = Settings.from_env({}, base_dir=tmp_path)
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1:18121") as client:
        initialized = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "test", "version": "1"}})
        assert initialized["serverInfo"]["name"] == "FrameNote"
        assert "Space" in initialized["instructions"]
        tools = {tool["name"]: tool for tool in rpc(client, "tools/list")["tools"]}
        assert set(tools) == TOOLS | {"prepare_video", "get_operation", "cancel_operation"}
        assert set(tools["transcribe_video"]["inputSchema"]["properties"]) == {"video_url", "force_asr"}
        assert tools["get_frame"]["annotations"]["readOnlyHint"] is True
        assert tools["extract_frames"]["annotations"]["readOnlyHint"] is False
        assert tools["transcribe_video"]["annotations"]["openWorldHint"] is True
        task = app.state.store.create(status="completed", title="离线材料", duration=5, assets={"video": "source.mp4"})
        task_id = task["task_id"]
        directory = app.state.store.directory(task_id)
        (directory / "source.mp4").write_bytes(video.read_bytes())
        atomic_json(directory / "transcript.json", {"duration": 5, "timestamp_kind": "VAD_interval",
                    "segments": [{"start": i, "end": i + 1, "text": f"段落{i}"} for i in range(5)]})
        original = (directory / "transcript.json").read_bytes()
        result = payload(call(client, "get_transcript", task_id=task_id, start=1.5, end=4, offset=1, limit=1,
                              output_format="markdown"))
        assert result["total_segments"] == 3
        assert result["segments"][0]["text"] == "段落2"
        assert result["timestamp_kind"] == "VAD_interval"
        assert "段落2" in result["content"]
        assert (directory / "transcript.json").read_bytes() == original
        frames = payload(call(client, "extract_frames", task_id=task_id, timestamps=[1.2]))["frames"]
        frame_id = frames[0]["frame_id"]
        assert abs(frames[0]["actual_time"] - 1.2) < .041
        image = call(client, "get_frame", task_id=task_id, frame_id=frame_id)["content"][0]
        assert image["type"] == "image" and image["mimeType"] == "image/jpeg"
        assert base64.b64decode(image["data"]).startswith(b"\xff\xd8")
        assert payload(call(client, "get_artifacts", task_id=task_id))["artifacts"]
        assert payload(call(client, "wait_for_task", task_id=task_id))["status"] == "completed"
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        assert payload(call(client, "list_tasks"))["total"] == 1
        assert payload(call(client, "get_task_status", task_id=task_id))["title"] == "离线材料"
        assert not call(client, "get_frame", task_id=task_id, frame_id=frame_id).get("isError")
    with TestClient(create_app(Settings.from_env({}, base_dir=tmp_path / "other")), base_url="http://localhost") as client:
        assert payload(call(client, "list_tasks"))["total"] == 0


def test_submit_cancel_and_recoverable_errors(tmp_path, monkeypatch):
    async def run(self, task_id, aborted):
        await asyncio.Event().wait()

    monkeypatch.setattr(Pipeline, "run", run)
    with TestClient(create_app(Settings.from_env({}, base_dir=tmp_path)), base_url="http://localhost") as client:
        task_id = payload(call(client, "transcribe_video", video_url="https://youtu.be/BaW_jenozKc"))["task_id"]
        assert "youtube.com/watch" in payload(call(client, "get_task_status", task_id=task_id))["video_url"]
        result = call(client, "get_transcript", task_id=task_id)
        assert result["isError"] and "409" in result["content"][0]["text"]
        payload(call(client, "cancel_task", task_id=task_id))
        assert payload(call(client, "wait_for_task", task_id=task_id, timeout_seconds=2))["status"] == "cancelled"
        assert call(client, "transcribe_video", video_url="/tmp/local.mp4")["isError"]
        for name, arguments in [
            ("get_task_status", {"task_id": "../secret"}),
            ("get_task_status", {"task_id": "a" * 32}),
            ("get_transcript", {"task_id": task_id, "limit": 501}),
            ("get_transcript", {"task_id": task_id, "start": -1}),
            ("wait_for_task", {"task_id": task_id, "timeout_seconds": 46}),
            ("extract_frames", {"task_id": task_id, "timestamps": [1] * 25}),
        ]:
            assert call(client, name, **arguments)["isError"]
        response = client.post("/mcp", headers={"Host": "attacker.example", "Accept": "application/json, text/event-stream"},
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 421


@pytest.mark.asyncio
async def test_wait_is_bounded_and_does_not_cancel_job():
    paths = []

    async def request(method, path, **kwargs):
        paths.append(path)
        return httpx.Response(200, json={"status": "processing", "task_id": "a" * 32})

    start = time.monotonic()
    result = await MaterialTools(request).wait_for_task("a" * 32, timeout_seconds=1)
    assert result["status"] == "processing"
    assert time.monotonic() - start < 1.5
    assert all(path.endswith("a" * 32) for path in paths)

    async def slow_request(method, path, **kwargs):
        await asyncio.sleep(10)

    start = time.monotonic()
    with pytest.raises(ValueError, match="same task_id"):
        await MaterialTools(slow_request).wait_for_task("a" * 32, timeout_seconds=1)
    assert time.monotonic() - start < 1.5


@pytest.mark.asyncio
async def test_stdio_configuration_and_real_protocol(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"tasks": [], "total": 0})

    original = httpx.AsyncClient

    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("FRAMENOTE_SERVICE_URL", "http://environment:8")
    await create_mcp(base_url="http://explicit:9").call_tool("list_tasks", {})
    assert requests[-1].startswith("http://explicit:9/api/tasks")
    await create_mcp().call_tool("list_tasks", {})
    assert requests[-1].startswith("http://environment:8/api/tasks")
    with pytest.raises(ValueError):
        create_mcp(app=create_app(Settings.from_env({}, base_dir=tmp_path)), base_url="http://invalid")
    async with stdio_client(StdioServerParameters(command=sys.executable, args=["-m", "framenote.service_mcp"])) as (read, write):
        async with ClientSession(read, write) as session:
            assert (await session.initialize()).serverInfo.name == "FrameNote"
            assert {tool.name for tool in (await session.list_tools()).tools} == TOOLS | {"prepare_video", "get_operation", "cancel_operation"}


@pytest.mark.asyncio
async def test_mcp_pending_frames_returns_operation_then_real_images():
    calls = []
    state = {"status": "processing"}
    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith("/frames/f1"):
            return httpx.Response(200, content=b"\xff\xd8actual-image")
        return httpx.Response(200, json={"operation_id": "b" * 32, "task_id": "a" * 32,
            "kind": "frames", "status": state["status"], "result": {"frames": [{"frame_id": "f1"}] if state["status"] == "completed" else []}})
    tools = MaterialTools(request)
    result = await tools.extract_frames("a" * 32, [12])
    assert result["operation"]["status"] == "processing"
    assert calls[0][2]["json"] == {"timestamps": [12], "background": True}
    assert calls[1][1].endswith("/operations/" + "b" * 32)
    assert calls[1][2]["params"]["wait"] == 0
    assert calls[2][2]["params"]["wait"] == 45
    state["status"] = "completed"
    op = await tools.get_operation("a" * 32, "b" * 32, timeout_seconds=0)
    assert op["result"]["frames"] == [{"frame_id": "f1"}]
    image = await tools.get_frame("a" * 32, "f1")
    assert image.data == b"\xff\xd8actual-image"
    assert (await tools.extract_frames("a" * 32, [12]))["frames"] == [{"frame_id": "f1"}]


@pytest.mark.asyncio
async def test_mcp_video_preparation_and_force_asr_forward_public_fields_only():
    calls = []
    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return httpx.Response(200, json={"operation_id": "b" * 32, "status": "completed",
                                       "result": {"downloads": {"video": "/api/task/a/video"}}})
    tools = MaterialTools(request)
    await tools.transcribe_video("https://b23.tv/example", force_asr=True)
    assert calls[0][2]["json"] == {"video_url": "https://b23.tv/example", "force_asr": True}
    prepared = await tools.prepare_video("a" * 32)
    assert prepared["operation"]["status"] == "completed"
    assert prepared["downloads"]["video"] == "/api/task/a/video"
    assert calls[-1] == ("POST", "/api/task/" + "a" * 32 + "/media", {"json": {"purpose": "video"}})
    await tools.cancel_operation("a" * 32, "b" * 32)
    assert calls[-1][1].endswith("/operations/" + "b" * 32 + "/cancel")


def test_mcp_retains_original_nine_tool_names_and_operation_annotations(tmp_path):
    with TestClient(create_app(Settings.from_env({}, base_dir=tmp_path)), base_url="http://localhost") as client:
        tools = {tool["name"]: tool for tool in rpc(client, "tools/list")["tools"]}
        assert TOOLS <= tools.keys()
        assert len(tools) == 12
        assert set(tools["transcribe_video"]["inputSchema"]["properties"]) == {"video_url", "force_asr"}
        assert tools["get_operation"]["annotations"]["readOnlyHint"] is True
        for name in ("extract_frames", "prepare_video"):
            assert tools[name]["annotations"]["openWorldHint"] is True
            assert tools[name]["annotations"]["readOnlyHint"] is False
        assert tools["cancel_operation"]["annotations"]["idempotentHint"] is True
        for timeout in (-1, 46):
            assert call(client, "get_operation", task_id="a" * 32, operation_id="b" * 32,
                        timeout_seconds=timeout)["isError"]


@pytest.mark.asyncio
async def test_mcp_operation_transport_timeout_preserves_resumable_id():
    async def request(method, path, **kwargs):
        if method == "POST":
            return httpx.Response(202, json={"operation_id": "b" * 32, "status": "processing"})
        raise asyncio.TimeoutError()
    tools = MaterialTools(request)
    assert (await tools.extract_frames("a" * 32, [1]))["operation"]["operation_id"] == "b" * 32
    with pytest.raises(TimeoutError, match="b" * 32):
        await tools.get_operation("a" * 32, "b" * 32)


@pytest.mark.asyncio
async def test_mcp_operation_wait_budget_returns_latest_pending_payload():
    operation = {"operation_id": "b" * 32, "status": "processing", "phase": "acquisition"}
    async def request(method, path, **kwargs):
        if kwargs["params"]["wait"]:
            raise asyncio.TimeoutError()
        return httpx.Response(200, json=operation)
    assert await MaterialTools(request).get_operation("a" * 32, "b" * 32, timeout_seconds=1) == operation
