# FrameNote

把 B站、抖音、YouTube 或本地视频变成可供 Agent 阅读、整理与继续讨论的材料。服务负责媒体获取、SenseVoiceSmall ONNX INT8 转录、任务持久化和独立截图；Codex 通过项目 Skill 写章节笔记和逐章原稿。

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

## 使用 Skill

仓库 `.agents/skills/framenote` 链接到 [Skill](skills/framenote/SKILL.md)。其他项目可复制整个 `skills/framenote` 到其 `.agents/skills/framenote`；用户级安装可复制到 `~/.agents/skills/framenote`。源码归档可建立项目入口：

```bash
mkdir -p .agents/skills
ln -s ../../skills/framenote .agents/skills/framenote
```

Skill 通过 `scripts/headless.py` 调用 HTTP 服务，客户端仅需 Python 标准库。在当前工作项目中运行脚本；安装位置与项目配置查找无关。

复制 [.framenote.example.json](.framenote.example.json) 为项目根目录的 `.framenote.json`，将 `service_url` 改为后端可达地址。同机使用 `http://127.0.0.1:18121`，异机使用 `http://SERVER:18121`。用户级默认值写入 `~/.config/framenote/client.json`，两者使用相同格式：

```json
{
  "service_url": "http://127.0.0.1:18121",
  "download_video": false
}
```

优先级：命令行参数 → 环境变量 → 最近的项目配置 → 用户配置 → 内置默认值，按字段覆盖。项目配置从执行目录向上寻找，最多到 Git 根目录；非 Git 目录查到文件系统根。相对 `notes_dir` 按配置文件所在目录解析；实际项目配置由 Git 忽略。完整规则见[配置指南](docs/CONFIGURATION.md)。

```bash
python3 -S skills/framenote/scripts/headless.py config
python3 -S skills/framenote/scripts/headless.py notes-dir
python3 -S skills/framenote/scripts/headless.py transcribe '<视频链接或本地路径>' --out-dir ./my-note --wait 1800
python3 -S skills/framenote/scripts/headless.py transcript TASK_ID --start 30 --end 90 --limit 200
python3 -S skills/framenote/scripts/headless.py frames TASK_ID --timestamps 30 90 --out-dir ./my-note
python3 -S skills/framenote/scripts/headless.py fetch TASK_ID --out-dir ./my-note --download-video
```

`tasks` 分页查找旧任务；`status`、`wait`、`cancel` 管理任务；`artifacts` 列出材料入口。转录每页最多 500 段，单次抽帧最多 24 张。Agent 读取下载到本机的转写与实际截图，再组织笔记。

视频自动取回默认关闭。配置 `download_video: true` 或设置 `FRAMENOTE_DOWNLOAD_VIDEO=true` 开启自动取回；`--download-video` / `--no-download-video` 覆盖单次执行。视频保存为 `materials/video.*`，信息记录在 `bundle.json`；重复取回会校验本地文件并通过 ETag 复用。视频沿用服务端已获取的画质；分离音轨按需合并为 MP4，源素材保留。

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
    ├── video.*                 # 开启取回时保存
    └── corrections.md
```

--out-dir 表示笔记根目录。frames.json.relative_path 相对该根目录，重取材料保留阅读稿与 corrections.md。原始转录和截图是整理依据，Skill 生成带章节时间、截图、原稿引用的阅读文档。

YouTube 支持普通视频、youtu.be 短链、Shorts、嵌入和已结束直播的视频链接；含播放列表参数的链接只处理当前视频。服务所在机器需能访问 YouTube，并安装 Deno ≥2.3 或 Node.js ≥22；详见[部署指南](docs/DEPLOYMENT.md#youtube-下载依赖)。

B站保留 ?p=N，默认第1P；B站、抖音和 YouTube 先尝试平台下载，按配置启用 Galaxy 外部 API 回退；本地视频流式上传，上限默认2GiB。源视频保留在服务 workspace/tasks/TASK_ID/，可继续截图。SenseVoice 时间为 VAD 语音片段区间，章节边界按近似范围表达。

Galaxy 外部 API 回退使用 [bhwa233/galaxy-downloader](https://github.com/bhwa233/galaxy-downloader) 项目的后端 API。通过 `FRAMENOTE_GALAXY_ENABLED=true` 启用 B站、抖音和 YouTube 的平台下载失败回退。`FRAMENOTE_GALAXY_BASE_URL` 配置后端 API 基址。Galaxy 返回的 HLS 会通过 ffmpeg 下载封装；服务检查实际轨道并按需保留独立音轨，供转写和视频取回使用。

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
