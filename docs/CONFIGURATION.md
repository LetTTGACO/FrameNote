# 配置

服务使用环境变量，service.env 由 shell 或 systemd 的 EnvironmentFile 加载。显式传给 create_app 的 Settings 优先于环境；其余使用环境值或下表默认值。路径先展开 ~，相对路径按启动 cwd 解析并固定。ASR Python 路径保留 venv 的符号链接身份。

| 环境变量 | 默认值与含义 |
| --- | --- |
| FRAMENOTE_WORKSPACE | workspace；任务在其 tasks/ 下 |
| FRAMENOTE_ASR_BACKEND | sensevoice；选择代码工厂中已登记的实现 |
| FRAMENOTE_SENSEVOICE_MODEL | models/sensevoice |
| FRAMENOTE_VAD_MODEL | models/vad |
| FRAMENOTE_ASR_PYTHON | 当前 sys.executable |
| FRAMENOTE_ASR_THREADS | 4，正整数 |
| FRAMENOTE_PROCESS_TIMEOUT | 7200 秒，正有限数；同时用于本地推理 |
| FRAMENOTE_MAX_UPLOAD_BYTES | 2147483648，正整数 |
| FRAMENOTE_GALAXY_ENABLED | false；true/yes/1 启用抖音获取失败回退 |
| FRAMENOTE_GALAXY_BASE_URL | https://downloader-api.bhwa233.com |
| FRAMENOTE_SERVICE_URL | 客户端和 stdio MCP：默认 http://127.0.0.1:18121 |
| FRAMENOTE_NOTES_DIR | Skill 笔记父目录覆盖；默认由 notes-dir 命令定位 |

默认在同一个 venv 安装服务与 .[asr]。覆盖 FRAMENOTE_ASR_PYTHON 时，目标 Python 同样需要安装 FrameNote 和 ASR 依赖；服务仍以独立子进程执行推理。可执行路径中有空格也可直接传入，不需要在变量值中添加 shell 引号。

## 加载与检查

```bash
set -a
. ./service.env
set +a
python -m uvicorn framenote.service:app --host 127.0.0.1 --port 18121 --workers 1 --no-proxy-headers
```

N100 局域网部署使用 --host 0.0.0.0。监听地址/端口由 uvicorn 或 systemd 的启动参数决定。

/api/health 始终返回进程活性与 backend 状态；asr_models_present 表示完整必需文件已存在。/api/ready 在配置、10 个文件及 Python 依赖预检均通过时返回 200，否则 503，并列出缺项。Python 依赖在启动时检查一次，安装或修复依赖后重启服务；模型完整哈希由显式 models verify 检查。健康请求只检查缓存依赖状态和文件存在性。

## 客户端与项目 MCP

脚本地址优先级为 --service-url > FRAMENOTE_SERVICE_URL > loopback。所有下载命令的 --out-dir 都表示笔记根目录，材料归 materials/。

```bash
python3 -S skills/framenote/scripts/headless.py notes-dir
python3 -S skills/framenote/scripts/headless.py --service-url http://127.0.0.1:18121 fetch TASK_ID --out-dir ./my-note
```

项目级 `.codex/config.toml` 在本地创建并由 Git 忽略，配置示例见 README；仓库提供 .agents/skills/framenote 相对链接。远程使用者将配置中的 url 改为实际地址，例如 http://SERVER:18121/mcp。挂载式 MCP 直接使用本服务，端口变化无需额外配置内部回调 URL。stdio MCP 通过 FRAMENOTE_SERVICE_URL 指向服务：

```bash
FRAMENOTE_SERVICE_URL=http://127.0.0.1:18121 python -m framenote.service_mcp
```

复制 Skill 时复制整个 skills/framenote 目录到目标项目的 .agents/skills/framenote。脚本仅需标准库，可直接使用。

[Codex 官方配置说明](https://developers.openai.com/codex/config-basic)说明项目配置优先于用户配置；修改通用示例之前保留本机实际使用的项目地址。CLI 可使用 --config 覆盖。
