import json
import sys

from mcp.types import ImageContent, TextContent
import pytest
import httpx

from framenote import service_mcp
from framenote.service import create_app
from framenote.service_settings import Settings, SenseVoiceConfig
from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_mcp_exposes_material_workflow_and_native_images(monkeypatch):
    paths = []

    async def request(method, path, **kwargs):
        paths.append((method, path, kwargs))
        if "/frames/" in path:
            return httpx.Response(200, content=b"jpeg-example", headers={"content-type": "image/jpeg"})
        return httpx.Response(200, json={"task_id": "a" * 32, "status": "completed"})

    monkeypatch.setattr(service_mcp, "request", request)
    tools = {tool.name for tool in await service_mcp.mcp.list_tools()}
    assert {"transcribe_video", "get_transcript", "extract_frames", "get_frame", "wait_for_task", "cancel_task"} <= tools
    result = await service_mcp.mcp.call_tool("get_frame", {"task_id": "a" * 32, "frame_id": "b" * 32})
    assert isinstance(result[0], ImageContent) and result[0].mimeType == "image/jpeg"
    await service_mcp.get_transcript("a" * 32, start=30, end=60, offset=2, limit=3)
    assert paths[-1][2]["params"] == {"start": 30, "end": 60, "offset": 2, "limit": 3, "output_format": "json"}
    task = await service_mcp.wait_for_task("a" * 32, timeout_seconds=0)
    assert task["status"] == "completed"


def test_remote_http_mcp_initialize_tools_and_native_image(tmp_path, monkeypatch):
    settings = Settings(workspace=tmp_path, sensevoice=SenseVoiceConfig(tmp_path / "sense", tmp_path / "vad", sys.executable))
    # Separate app instances must have independent session manager lifetimes.
    for _ in range(2):
        app = create_app(settings)
        with TestClient(app, client=("192.168.31.42", 51000)) as client:
            task = app.state.store.create(status="completed")
            task_id = task["task_id"]
            frame_id = "b" * 32
            frames = app.state.store.directory(task_id) / "frames"
            frames.mkdir()
            (frames / (frame_id + ".jpg")).write_bytes(b"jpeg-example")
            headers = {"Accept": "application/json, text/event-stream", "Host": "192.168.31.56:18121"}
            initialized = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "LAN test", "version": "1"}}})
            assert initialized.status_code == 200
            assert initialized.json()["result"]["serverInfo"]["name"] == "FrameNote"
            tools = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            assert tools.status_code == 200
            assert len(tools.json()["result"]["tools"]) == 9
            image = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "get_frame", "arguments": {"task_id": task_id, "frame_id": frame_id}}})
            assert image.status_code == 200
            content = image.json()["result"]["content"]
            assert content[0]["type"] == "image" and content[0]["mimeType"] == "image/jpeg"


def test_mounted_mcp_uses_own_app_without_network(tmp_path,monkeypatch):
    monkeypatch.setenv("FRAMENOTE_SERVICE_URL","http://127.0.0.1:1")
    seen=[]
    for index in range(2):
        settings=Settings.from_env({},base_dir=tmp_path/str(index))
        app=create_app(settings)
        with TestClient(app) as client:
            task=app.state.store.create(title=f"app-{index}",status="completed")
            response=client.post("/mcp",headers={"Accept":"application/json, text/event-stream"},json={"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_task_status","arguments":{"task_id":task["task_id"]}}})
            assert response.status_code==200
            result=response.json()["result"]
            assert not result.get("isError",False),result
            payload=json.loads(result["content"][0]["text"])
            assert payload["task_id"]==task["task_id"]
            assert payload["title"]==f"app-{index}"
            seen.append(payload["task_id"])
    assert len(set(seen))==2


@pytest.mark.asyncio
async def test_stdio_url_precedence_and_nine_tools(monkeypatch):
    requests=[]
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200,json={"tasks":[],"total":0})
    original=httpx.AsyncClient
    def client(**kwargs):
        kwargs["transport"]=httpx.MockTransport(handler)
        return original(**kwargs)
    monkeypatch.setattr(service_mcp.httpx,"AsyncClient",client)
    monkeypatch.setenv("FRAMENOTE_SERVICE_URL","http://environment:8")
    explicit=service_mcp.create_mcp(base_url="http://explicit:9")
    await explicit.call_tool("list_tasks",{})
    assert requests[-1].startswith("http://explicit:9/api/tasks")
    await service_mcp.create_mcp().call_tool("list_tasks",{})
    assert requests[-1].startswith("http://environment:8/api/tasks")
    monkeypatch.delenv("FRAMENOTE_SERVICE_URL")
    await service_mcp.create_mcp().call_tool("list_tasks",{})
    assert requests[-1].startswith("http://127.0.0.1:18121/api/tasks")
    tools={tool.name:tool for tool in await explicit.list_tools()}
    assert set(tools)=={"transcribe_video","get_task_status","wait_for_task","list_tasks","get_transcript","extract_frames","get_frame","get_artifacts","cancel_task"}
    assert tools["wait_for_task"].inputSchema["properties"]["timeout_seconds"]["default"]==45
    assert tools["get_transcript"].inputSchema["properties"]["limit"]["default"]==200
    with pytest.raises(ValueError):
        service_mcp.create_mcp(app=create_app(Settings.from_env({})),base_url="http://invalid")
