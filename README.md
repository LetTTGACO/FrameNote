# FrameNote

将B站、抖音或本地视频变成可供Codex阅读和继续讨论的材料。Homelab负责下载、SenseVoiceSmall转录和独立抽帧，Codex负责首次整理的章节笔记。

## 工作流

视频链接/本地视频 → Homelab材料服务 → 转录与截图 → Codex章节笔记 → 按原稿和截图继续讨论。

每个视频的本地输出：

```text
视频目录/
├── notes.md
├── chapters-transcript.md
└── materials/
    ├── frames/
    ├── frames.json
    ├── transcript.json
    ├── transcript.md
    ├── transcript.srt
    ├── asr.raw.json
    ├── metadata.json
    ├── bundle.json
    └── corrections.md
```

`corrections.md`按需保存术语核对与疑点。frames.json的relative_path相对笔记根目录，原始ASR材料保持原样。服务保留源视频，以task_id回查或追加截图。

## 安装与运行

需要Python3.10+、ffmpeg、ffprobe、curl。服务和ASR可使用同一Python环境，也可通过FRAMENOTE_ASR_PYTHON指定本机独立ASR环境。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

ASR环境安装 `.[asr]`，配置已有SenseVoiceSmall和FSMN VAD ONNX INT8模型目录，目录中需要model_quant.onnx及对应配置和词表。配置示例见[deploy/service.env.example](deploy/service.env.example)。

在项目目录准备service.env，修改路径与服务地址后加载：

```bash
set -a
. ./service.env
set +a
.venv/bin/python -m uvicorn framenote.service:app --host 0.0.0.0 --port 18121 --workers 1 --no-proxy-headers
```

[systemd模板](deploy/framenote.service)使用 `/root/labs/FrameNote`。更换安装位置时同步修改WorkingDirectory、EnvironmentFile和ExecStart。

## MCP与Skill

Homelab地址：`http://192.168.31.56:18121`，MCP为同端口的 `/mcp`，协议Streamable HTTP。FRAMENOTE_SERVICE_URL是MCP访问REST的基址，更换地址/端口时同步配置。

MCP工具：transcribe_video、get_task_status、wait_for_task、list_tasks、get_transcript、extract_frames、get_frame、get_artifacts、cancel_task。每次等待最多45秒，转录分页上限500段，单次抽帧最多24张。

[Skill](skills/framenote/SKILL.md)描述章节笔记写作与回查方式。本地上传和材料下载使用标准库客户端，--out-dir传笔记根目录：

```bash
python3 skills/framenote/scripts/headless.py transcribe '<B站/抖音链接或本地视频路径>' --out-dir workspace/notes/my-video
python3 skills/framenote/scripts/headless.py frames '<task_id>' --timestamps 30 90 --out-dir workspace/notes/my-video
python3 skills/framenote/scripts/headless.py fetch '<task_id>' --out-dir workspace/notes/my-video
```

服务地址可用FRAMENOTE_SERVICE_URL或--service-url覆盖。Codex项目配置位于 `.codex/config.toml`，Skill入口位于 `.agents/skills/framenote`，均在项目范围内。

## 材料与任务

B站保留?p=N，缺省处理第1P。抖音先尝试平台下载器，再按FRAMENOTE_GALAXY_ENABLED使用Galaxy API；该回退依赖作者的外部解析服务。原视频和音轨保存在Homelab的workspace/tasks/<task_id>/，Mac取得转录与截图。

SenseVoice时间来自VAD片段区间，每段最长30秒；章节时间按近似范围表达。截图返回requested_time和实际解码帧actual_time。

单worker串行处理，任务以JSON持久化。取消请求立即标记任务，队列等待整组子进程回收后处理下一任务；排队任务在重启后继续；处理中断任务的status为failed、phase为interrupted，已落盘材料保留。上传上限2GiB。

## 开发与来源

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy framenote
```

由[like-attract/video-to-note](https://github.com/like-attract/video-to-note)的无界面实验版本提取。复用的平台下载与转录渲染代码保留MIT许可和来源，见[NOTICE](NOTICE)、[LICENSE](LICENSE)。实施范围见[docs/SCOPE.md](docs/SCOPE.md)，实际结果见[验收记录](docs/VALIDATION.md)，机器路径与运维入口见[当前部署](docs/DEPLOYMENT.md)。
