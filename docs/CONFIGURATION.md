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
| FRAMENOTE_GALAXY_ENABLED | false；true/yes/1 启用平台获取失败回退 |
| FRAMENOTE_GALAXY_BASE_URL | https://downloader-api.bhwa233.com |
| FRAMENOTE_SERVICE_URL | 客户端服务基址，默认 http://127.0.0.1:18121 |
| FRAMENOTE_NOTES_DIR | 客户端笔记父目录覆盖 |
| FRAMENOTE_DOWNLOAD_VIDEO | 客户端自动取回视频，默认 false |

默认在同一个 venv 安装服务与 .[asr]。覆盖 FRAMENOTE_ASR_PYTHON 时，目标 Python 同样需要安装 FrameNote 和 ASR 依赖；服务仍以独立子进程执行推理。可执行路径中有空格也可直接传入，不需要在变量值中添加 shell 引号。

## Galaxy 回退

B站、抖音和 YouTube 优先使用平台下载，失败时尝试 Galaxy。总开关默认关闭，启用方式：

```text
FRAMENOTE_GALAXY_ENABLED=true
```

`/api/health` 返回 Galaxy 回退的开关状态。

HLS 播放列表通过 ffmpeg 下载封装，直链媒体通过 curl 下载。服务依据 ffprobe 检测结果判断是否需要独立音频，保留已完成素材并清理失败或取消的临时下载。Galaxy 解析响应中的临时下载地址不进入公开元数据；服务所在机器仍需能访问返回的媒体 CDN。

## 加载与检查

```bash
set -a
. ./service.env
set +a
python -m uvicorn framenote.service:app --host 127.0.0.1 --port 18121 --workers 1 --no-proxy-headers
```

N100 局域网部署使用 --host 0.0.0.0。监听地址/端口由 uvicorn 或 systemd 的启动参数决定。

/api/health 始终返回进程活性与 backend 状态；asr_models_present 表示完整必需文件已存在。/api/ready 在配置、10 个文件及 Python 依赖预检均通过时返回 200，否则 503，并列出缺项。Python 依赖在启动时检查一次，安装或修复依赖后重启服务；模型完整哈希由显式 models verify 检查。健康请求只检查缓存依赖状态和文件存在性。

## Skill 客户端配置

`headless.py` 在 Agent 所在机器运行，使用 Python 标准库。客户端配置由脚本读取，`service.env` 用于后端进程。

| 字段 | 含义 |
| --- | --- |
| `service_url` | HTTP API 基址，例如 `http://SERVER:18121` |
| `notes_dir` | 笔记父目录，可省略；相对路径按配置文件父目录解析，支持 `~` |
| `download_video` | JSON 布尔值；自动取回服务端视频，默认 `false` |

用户级配置为 `~/.config/framenote/client.json`。项目级配置为 `.framenote.json`，模板在仓库根的 `.framenote.example.json`。脚本从执行时的 cwd 向上查找最近一份项目配置，遇到 Git 根目录（含 worktree 的 `.git` 文件）停止；非 Git 目录查到文件系统根。仅使用最近一份项目配置；未填写的字段继承用户配置。

配置优先级：`命令行参数 > 环境变量 > 项目配置 > 用户配置 > 内置默认值`。字段对应 `--service-url` / `FRAMENOTE_SERVICE_URL`、`--notes-dir` / `FRAMENOTE_NOTES_DIR`、`--download-video` 或 `--no-download-video` / `FRAMENOTE_DOWNLOAD_VIDEO`。参数可位于子命令前后；环境布尔值接受 true/false、yes/no、1/0、on/off。未知字段、类型错误和无效 JSON 会明确报错。

```json
{
  "service_url": "http://SERVER:18121",
  "notes_dir": "./video-notes",
  "download_video": false
}
```

Skill 安装目录不参与配置或默认笔记目录查找。在目标项目运行：

```bash
python3 -S <Skill目录>/scripts/headless.py config
python3 -S <Skill目录>/scripts/headless.py notes-dir
```

`config` 显示生效值、项目/用户配置路径与各字段来源。无 `notes_dir` 时，本仓库默认 `workspace/notes/`，其他 Git 仓库默认仓库根的 `framenote-notes/`，非 Git 目录默认 cwd 下的 `framenote-notes/`。参数或环境变量中的相对路径按执行 cwd 解析。下载命令的 `--out-dir` 始终显式指定笔记根目录，材料归 `materials/`。

## 视频取回

任务状态和材料清单在视频可用时返回 `downloads.video`，例如 `/api/task/TASK_ID/video`；服务端绝对路径不参与客户端定位。GET 此入口流式返回文件，支持 ETag 条件请求。视频与音轨分离时，服务复用保留音轨生成缓存 MP4；源素材保留，画质沿用已获取的视频。

```bash
python3 -S <Skill目录>/scripts/headless.py fetch TASK_ID --out-dir ./my-note --download-video
python3 -S <Skill目录>/scripts/headless.py transcribe '<链接或本地路径>' --out-dir ./my-note --no-download-video
```

开启取回时，视频存入 `materials/video.*`，`bundle.json` 记录路径、字节数、SHA-256 与 ETag。重复取回校验本地文件后复用服务端未变化的版本；下载失败清理 `.part`，保留原有完整文件，`download_complete` 保持 false，重试 `fetch` 即可。关闭取回保留此前已下载的视频。转写、截图、任务身份与阅读文档的保护规则继续适用。
