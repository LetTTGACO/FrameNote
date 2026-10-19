"""Shared MCP tools for the Homelab HTTP endpoint and stdio entry point."""
from __future__ import annotations

import asyncio
import os
import time

import httpx
from mcp.server.fastmcp import FastMCP, Image

INSTRUCTIONS = (
    "准备视频材料：transcribe_video提交B站/抖音链接，wait_for_task每次最多等45秒，"
    "get_transcript按时间/分页读取。SenseVoice时间为VAD片段区间。"
    "extract_frames独立抽帧，get_frame返回原生图像；由你根据材料组织章节与笔记。"
    "Mac本地视频上传和材料下载使用随仓库提供的headless Skill脚本。")


async def request(method: str, path: str, **kwargs):
    base = os.getenv("FRAMENOTE_SERVICE_URL", "http://192.168.31.56:18121").rstrip("/")
    async with httpx.AsyncClient(base_url=base, timeout=60, trust_env=False) as client:
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        return response


async def transcribe_video(video_url: str) -> dict:
    """提交B站/抖音单视频链接；保留视频供后续独立抽帧。"""
    return (await request("POST", "/api/transcribe", json={"video_url": video_url})).json()


async def get_task_status(task_id: str) -> dict:
    """读取进度、获取路径、结果和错误。"""
    return (await request("GET", f"/api/task/{task_id}")).json()


async def wait_for_task(task_id: str, timeout_seconds: int = 45) -> dict:
    """有界等待终态；未完成时可再次调用，最多等待45秒。"""
    deadline = time.monotonic() + max(1, min(45, timeout_seconds))
    while True:
        task = await asyncio.wait_for(get_task_status(task_id), timeout=max(.1, deadline - time.monotonic()))
        if task["status"] in {"completed", "failed", "cancelled"} or time.monotonic() >= deadline:
            return task
        await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))


async def list_tasks(offset: int = 0, limit: int = 20) -> dict:
    """分页读取持久任务，包括Agent提交的任务。"""
    return (await request("GET", "/api/tasks", params={"offset": offset, "limit": limit})).json()


async def get_transcript(task_id: str, start: float = 0, end: float | None = None,
                         offset: int = 0, limit: int = 200, output_format: str = "json") -> dict:
    """按时间范围和分页读取原稿；output_format可选json、markdown、srt。"""
    params = {"start": start, "offset": offset, "limit": limit, "output_format": output_format}
    if end is not None:
        params["end"] = end
    return (await request("GET", f"/api/task/{task_id}/transcript", params=params)).json()


async def extract_frames(task_id: str, timestamps: list[float]) -> dict:
    """按秒抽帧，每次最多24张；返回请求时间与实际帧PTS。"""
    return (await request("POST", f"/api/task/{task_id}/frames", json={"timestamps": timestamps})).json()


async def get_frame(task_id: str, frame_id: str) -> Image:
    """读取一张截图，返回可直接查看的原生图像内容。"""
    response = await request("GET", f"/api/task/{task_id}/frames/{frame_id}")
    return Image(data=response.content, format="jpeg")


async def get_artifacts(task_id: str) -> dict:
    """列出可下载材料的HTTP路径；用Skill脚本将材料落到Mac。"""
    return (await request("GET", f"/api/task/{task_id}/artifacts")).json()


async def cancel_task(task_id: str) -> dict:
    """取消排队或运行中的任务，保留已落盘材料。"""
    return (await request("POST", f"/api/task/{task_id}/cancel")).json()


def create_mcp() -> FastMCP:
    server = FastMCP("FrameNote", instructions=INSTRUCTIONS, host="0.0.0.0",
                     stateless_http=True, json_response=True)
    for tool in (transcribe_video, get_task_status, wait_for_task, list_tasks, get_transcript,
                 extract_frames, get_frame, get_artifacts, cancel_task):
        server.add_tool(tool)
    return server


mcp = create_mcp()


if __name__ == "__main__":
    mcp.run(transport="stdio")
