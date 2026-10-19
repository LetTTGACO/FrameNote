# FrameNote

把 B站、抖音或本地视频变成可供 Agent 阅读、整理与继续讨论的材料。服务负责媒体获取、SenseVoiceSmall ONNX INT8 转录、任务持久化和独立截图；Codex 通过项目 Skill 写章节笔记和逐章原稿。

## 快速运行

支持 Python 3.10–3.12，原生部署目标为 Ubuntu x86_64 与 Apple Silicon macOS arm64。系统先安装 ffmpeg/ffprobe、curl 和音频库，平台命令见 [部署指南](docs/DEPLOYMENT.md)。

```bash
git clone https://github.com/LetTTGACO/FrameNote.git
cd FrameNote
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[asr]'
python -m framenote.models prepare --out models
python -m framenote.models verify --out models
cp deploy/service.env.example service.env
set -a
. ./service.env
set +a
python -m uvicorn framenote.service:app --host 127.0.0.1 --port 18121 --workers 1 --no-proxy-headers
```

默认一个 venv，推理在独立子进程中运行。模型准备命令从公开固定版本下载全部 10 个文件并校验 SHA-256。另一终端检查 `curl --fail http://127.0.0.1:18121/api/ready`；N100 局域网和 systemd 配置见部署指南。

## 接入 MCP 与 Skill

项目 .codex/config.toml 示例为 `http://127.0.0.1:18121/mcp`，协议为 Streamable HTTP；远程使用时替换为客户端可达地址。仓库 .agents/skills/framenote 链接项目 [Skill](skills/framenote/SKILL.md)。从源码归档安装时，可建立同样入口：

```bash
mkdir -p .agents/skills
ln -s ../../skills/framenote .agents/skills/framenote
```

其他项目可复制整个 skills/framenote 到其 .agents/skills/framenote。上传和材料下载脚本仅使用 Python 标准库，服务可部署在另一台机器：

```bash
python3 -S skills/framenote/scripts/headless.py --service-url http://127.0.0.1:18121 transcribe '<视频链接或本地路径>' --out-dir ./my-note
python3 -S skills/framenote/scripts/headless.py --service-url http://127.0.0.1:18121 frames TASK_ID --timestamps 30 90 --out-dir ./my-note
python3 -S skills/framenote/scripts/headless.py --service-url http://127.0.0.1:18121 fetch TASK_ID --out-dir ./my-note
```

9 个工具：transcribe_video、get_task_status、wait_for_task、list_tasks、get_transcript、extract_frames、get_frame、get_artifacts、cancel_task。wait 最多45秒，转录每页最多500段，单次抽帧最多24张；get_frame 返回原生图像。stdio 入口为 `python -m framenote.service_mcp`，服务基址由 FRAMENOTE_SERVICE_URL 配置。

## 阅读材料

```text
my-note/
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

--out-dir 表示笔记根目录。frames.json.relative_path 相对该根目录，重取材料保留阅读稿与 corrections.md。原始转录和截图是整理依据，Skill 生成带章节时间、截图、原稿引用的阅读文档。

B站保留 ?p=N，默认第1P；抖音先尝试平台下载，按配置启用 Galaxy 外部 API 回退；本地视频流式上传，上限默认2GiB。源视频保留在服务 workspace/tasks/TASK_ID/，可继续截图。SenseVoice 时间为 VAD 语音片段区间，章节边界按近似范围表达。

单 worker 串行转录，JSON 持久化与文件锁保证 workspace 独占。排队任务重启继续，处理中断标记 failed/interrupted并保留已落盘材料；取消等待进程组清理后再处理下一任务。

## 配置、扩展与开发

[配置项](docs/CONFIGURATION.md)、[模型来源与准备](docs/MODELS.md)、[ASR 开发者接口](docs/ASR.md)、[部署/升级/备份](docs/DEPLOYMENT.md)。新增后端通过实现协议、解析配置、登记工厂和契约测试接入；Pipeline 消费带时间段落的规范结果。

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m mypy framenote
python -m build
```

CI 检查 Python 3.10/3.12、发行物内容与源码目录外的基础安装；两台真实机器的完整 ASR 安装/推理单独记录。

FrameNote 由 [like-attract/video-to-note](https://github.com/like-attract/video-to-note) 无界面实验版本提取，保留 MIT [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。模型与运行依赖分别保留上游来源与许可。当前范围见 [SCOPE](docs/SCOPE.md)，早期材料验收事实见 [VALIDATION](docs/VALIDATION.md)。
