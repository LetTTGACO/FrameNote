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
    async def request(method, path, **kwargs):
        return httpx.Response(200, content=b"jpeg-example", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(service_mcp, "request", request)
    settings = Settings(workspace=tmp_path, sensevoice=SenseVoiceConfig(tmp_path / "sense", tmp_path / "vad", sys.executable))
    # Separate app instances must have independent session manager lifetimes.
    for _ in range(2):
        app = create_app(settings)
        with TestClient(app, client=("192.168.31.42", 51000)) as client:
            headers = {"Accept": "application/json, text/event-stream", "Host": "192.168.31.56:18121"}
            initialized = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "LAN test", "version": "1"}}})
            assert initialized.status_code == 200
            assert initialized.json()["result"]["serverInfo"]["name"] == "FrameNote"
            tools = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            assert tools.status_code == 200
            assert len(tools.json()["result"]["tools"]) == 9
            image = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "get_frame", "arguments": {"task_id": "a" * 32, "frame_id": "b" * 32}}})
            assert image.status_code == 200
            content = image.json()["result"]["content"]
            assert content[0]["type"] == "image" and content[0]["mimeType"] == "image/jpeg"
