# FrameNote

FrameNote 可以将 B站、抖音、YouTube 或本地视频整理成可阅读、可追溯的章节笔记。B站优先读取平台字幕轨，轨道不可用时获取媒体并运行 ASR；截图和有声视频按需准备。HTTP 服务保留可追溯的原始材料；Agent 通过 Skill 客户端或 MCP 读取材料，撰写章节笔记和分章原稿。

从本页完成服务启动和 Skill 配置后，你可以发送视频链接开始整理。服务器运维见[部署指南](docs/DEPLOYMENT.md)，日常命令见[使用指南](docs/USAGE.md)。

## 准备运行环境

在运行服务的机器上准备以下依赖：

- Python 3.10–3.12。原生部署目标为 Ubuntu x86_64 和 Apple Silicon macOS arm64。
- `ffmpeg`、`ffprobe`、`curl` 和 `libsndfile`。安装命令见[安装系统依赖](docs/DEPLOYMENT.md#安装系统依赖)。
- 可访问模型下载源和目标视频平台的网络。
- 如需处理 YouTube，准备[YouTube 下载依赖](docs/DEPLOYMENT.md#youtube-下载依赖)。

服务与自动语音识别（ASR）依赖安装在同一个虚拟环境中，推理在独立子进程中运行。客户端只需要 Python 标准库，可以与服务运行在不同机器上。

## 启动服务

在服务机器的终端中完成以下步骤：

1. 获取源码：

   ```bash
   git clone https://github.com/LetTTGACO/FrameNote.git
   ```

2. 进入仓库：

   ```bash
   cd FrameNote
   ```

3. 创建虚拟环境：

   ```bash
   python3 -m venv .venv
   ```

4. 激活虚拟环境：

   ```bash
   . .venv/bin/activate
   ```

5. 安装服务和 ASR 依赖：

   ```bash
   python -m pip install '.[asr]'
   ```

6. 准备模型：

   ```bash
   python -m framenote.models prepare --out models
   ```

   命令下载并校验固定清单中的 10 个文件，成功后输出
   `Verified all model assets`。离线复核和来源见[模型指南](docs/MODELS.md)。

7. 首次配置时，复制环境变量模板：

   ```bash
   cp deploy/service.env.example service.env
   ```

   默认路径相对仓库根目录。已有 `service.env` 时，保留自定义值；各项含义见[配置参考](docs/CONFIGURATION.md)。

8. 将配置加载到终端环境：

   ```bash
   set -a
   . ./service.env
   set +a
   ```

9. 启动单 worker 服务：

   ```bash
   python -m uvicorn framenote.service:app \
     --host 127.0.0.1 --port 18121 --workers 1 --no-proxy-headers
   ```

10. 在另一个终端检查服务是否就绪：

    ```bash
    curl --fail http://127.0.0.1:18121/api/ready
    ```

    成功时返回 HTTP `200`，响应中的 `configured` 和 `available` 都为 `true`。
    如果返回 `503`，按 `reason` 修复模型或依赖，参见[排障](docs/DEPLOYMENT.md#排障)。

## 安装并配置 Skill

[FrameNote Skill](skills/framenote/SKILL.md) 和客户端位于 `skills/framenote/`。
在 FrameNote 仓库中使用时，按以下步骤建立入口：

1. 创建项目 Skill 目录：

   ```bash
   mkdir -p .agents/skills
   ```

2. 如果 `.agents/skills/framenote` 尚不存在，创建链接：

   ```bash
   ln -s ../../skills/framenote .agents/skills/framenote
   ```

3. 首次配置时，复制客户端模板：

   ```bash
   cp .framenote.example.json .framenote.json
   ```

4. 将 `.framenote.json` 中的 `service_url` 设为客户端可达的服务地址。
   同机运行可使用这个配置：

   ```json
   {
     "service_url": "http://127.0.0.1:18121",
     "download_video": false
   }
   ```

   跨机器运行时，填写服务器地址，并按[部署指南](docs/DEPLOYMENT.md#配置-systemd-服务)配置监听地址。

5. 在客户端的工作项目中检查配置：

   ```bash
   python3 -S skills/framenote/scripts/headless.py config
   ```

   确认 `service_url` 指向服务，`notes_dir` 指向任务工作区父目录。客户端将材料下载到 `materials/`，阅读文档由 Agent 撰写。

其他项目可把整个 `skills/framenote/` 复制到该项目的 `.agents/skills/framenote/`；用户级安装位置为 `~/.agents/skills/framenote/`。脚本按执行目录查找项目配置，Skill 安装位置不影响配置查找。

## 整理第一个视频

在已加载 Skill 的 Codex 对话中发送视频链接或本地视频路径。Agent 通过客户端获取转录和截图，阅读材料后直接撰写 `notes.md` 和 `chapters-transcript.md`；已有视频笔记继续在原入口维护。主笔记包含速览、引用资料、议题、后续讨论和证据回查。

要先用命令行检查材料获取，在仓库根目录运行：

```bash
python3 -S skills/framenote/scripts/headless.py transcribe 'VIDEO_SOURCE' \
  --out-dir ./my-note --wait 1800
```

将 `VIDEO_SOURCE` 替换为视频链接或本地视频路径。成功后，材料保存在 `my-note/materials/`；阅读文档由 Agent 根据这些材料撰写。检查 `materials/bundle.json` 中的任务状态是否为 `completed`、`download_complete` 是否为 `true`。

字幕任务的 `completed` 表示文字材料已完成，此时服务端可以没有视频、音频或 ASR 原件。截图请求会后台准备视频；取回有声视频时再补齐需要的音轨。媒体操作失败或取消不会撤销已完成的转录。需要重新识别时使用 `transcribe --force-asr` 创建新任务，并为新任务选择新的笔记目录。

视频自动取回默认关闭。B站登录仅在服务端通过[私有凭据管理](docs/CONFIGURATION.md#b站私有凭据管理)配置，服务应限制在受信网络或受保护隧道内。转录分页、任务管理、截图和视频取回见[使用指南](docs/USAGE.md)。

## 继续阅读

在 ChatGPT 网页和手机端使用时，通过 OpenAI Secure MCP Tunnel 连接服务器的
`http://127.0.0.1:18121/mcp`，创建私有插件。`plugins/framenote/` 中的 Skill
指导 ChatGPT 阅读视频，再通过 Obsidain MCP 读取知识库的 `Videos.md`，按其中规定的产物、位置和文档结构归档。
连接、宿主源文件准备与插件配置见 [MCP 指南](docs/MCP.md)。

按你要完成的任务选择文档：

| 任务 | 文档 |
| --- | --- |
| 获取材料、管理任务、截图和取回视频 | [使用指南](docs/USAGE.md) |
| 设置服务环境变量和客户端配置 | [配置参考](docs/CONFIGURATION.md) |
| 安装、升级、备份和排障 | [部署指南](docs/DEPLOYMENT.md) |
| 查询版本、归档发布包和回滚 | [版本发布与回滚](docs/RELEASING.md)、[发布记录](CHANGELOG.md) |
| 下载模型、核验哈希和查找许可来源 | [模型指南](docs/MODELS.md) |
| 理解服务、客户端和 Agent 的分工 | [系统范围](docs/SCOPE.md) |
| 接入其他 ASR 后端 | [ASR 契约与扩展指南](docs/ASR.md) |
| 修改代码并运行检查 | [贡献与 Agent 指南](AGENTS.md) |
| 核对 0.1.0 的历史验收证据 | [材料验收记录](docs/VALIDATION.md)、[原生部署验收记录](docs/VALIDATION-NATIVE.md) |

## 来源与许可

FrameNote 从 [video-to-note](https://github.com/like-attract/video-to-note) 的无界面实验版本提取，保留 MIT [许可](LICENSE)和[上游归属声明](NOTICE)。模型与运行依赖使用各自的上游许可，参见[模型来源与许可](docs/MODELS.md#来源与许可)。
