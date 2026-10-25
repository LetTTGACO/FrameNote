# 部署和维护 FrameNote

本指南供自行运行 FrameNote 的部署者使用，覆盖原生安装、systemd 服务、升级、备份和恢复。完成安装后，你可以通过就绪检查和真实视频任务验证服务。客户端操作见[使用指南](USAGE.md)。

## 开始之前

准备以下环境和文件：

- Ubuntu x86_64 / Python 3.10，或 Apple Silicon macOS arm64 / Python 3.12。
- FrameNote 源码，或 wheel 加源码归档中的 `deploy/` 模板。wheel 包含运行代码和模型锁；Skill、部署模板及文档随源码归档交付。
- 可供服务写入的 workspace，以及可访问模型源和视频平台的网络。
- 使用 systemd 时，准备创建服务用户、目录和 unit 所需的管理员权限。

服务和自动语音识别（ASR）依赖默认装在同一个虚拟环境中，推理在独立子进程执行。已有实例运行时，为候选实例选择独立 workspace、端口和 unit；本文使用 `18121` 作为正式端口，候选验收可使用 `18122`。

## 安装系统依赖

根据操作系统选择一组步骤，在源码根目录或独立运行目录执行。

### Ubuntu

在提供 Python 3.10 的发行版（例如 Ubuntu 22.04）上执行以下步骤：

1. 更新软件包索引：

   ```bash
   sudo apt-get update
   ```

2. 安装 Python 和媒体工具：

   ```bash
   sudo apt-get install -y python3.10 python3.10-venv ffmpeg curl libsndfile1
   ```

3. 创建虚拟环境：

   ```bash
   python3.10 -m venv .venv
   ```

### Apple Silicon macOS

使用 Homebrew 完成以下步骤：

1. 安装 Python 和媒体工具：

   ```bash
   brew install python@3.12 ffmpeg libsndfile
   ```

2. 创建虚拟环境：

   ```bash
   /opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
   ```

### 核对工具

在运行服务的终端中检查版本：

```bash
.venv/bin/python --version
ffmpeg -version
ffprobe -version
curl --version
```

确认 Python 为 3.10–3.12，其他命令均可执行。ASR 的四项固定依赖版本见 [pyproject.toml](../pyproject.toml)，两平台的历史安装结果见[原生部署验收记录](VALIDATION-NATIVE.md)。

## YouTube 下载依赖

YouTube 下载使用 `yt-dlp[default]`，该依赖提供匹配的 `yt-dlp-ejs`。
FrameNote 为 yt-dlp 启用 `PATH` 中的 Deno 和 Node.js。可用运行时需满足 Deno ≥2.3 或 Node.js ≥22，参见 [yt-dlp EJS 指南](https://github.com/yt-dlp/yt-dlp/wiki/EJS)。

macOS 可安装 Deno：

```bash
brew install deno
```

Ubuntu 按 [Deno 安装说明](https://docs.deno.com/runtime/getting_started/installation/)安装，或使用已有的 Node.js ≥22。安装后，以运行服务的用户执行 `deno --version` 或 `node --version`，确认版本和可执行路径。

使用 systemd 时，确保 unit 的 `PATH` 包含运行时所在目录。交互式用户目录中的可执行文件不会自动出现在服务用户的 `PATH` 中。

服务机器还需能访问 YouTube 和媒体 CDN。获取失败时，查看任务目录中的 `acquisition.log`，检查网络、yt-dlp 版本和平台访问限制。

## 安装服务并准备模型

在已创建 `.venv` 的目录完成以下步骤：

1. 激活虚拟环境：

   ```bash
   . .venv/bin/activate
   ```

2. 从源码根目录安装服务及 ASR 依赖：

   ```bash
   python -m pip install '.[asr]'
   ```

   如果使用 wheel，在独立运行目录改用这条命令：

   ```bash
   python -m pip install 'WHEEL_PATH[asr]'
   ```

   将 `WHEEL_PATH` 替换为 wheel 的绝对路径，保留 `[asr]`，例如以
   `framenote-0.2.1-py3-none-any.whl` 结尾的文件路径。

3. 检查依赖一致性：

   ```bash
   python -m pip check
   ```

   成功时输出 `No broken requirements found.`。

4. 下载模型：

   ```bash
   python -m framenote.models prepare --out models
   ```

5. 离线核验模型：

   ```bash
   python -m framenote.models verify --out models
   ```

   两个模型命令成功时均输出 `Verified all model assets`。来源、重试与自定义清单见[模型指南](MODELS.md)。

6. 首次配置时，将源码或源码归档中的 `deploy/service.env.example` 复制为运行目录的 `service.env`：

   ```bash
   cp deploy/service.env.example service.env
   ```

   这条命令从源码根目录执行。使用独立运行目录时，改用模板的实际路径。已有配置时保留自定义值，按[配置参考](CONFIGURATION.md)核对模型和 workspace 路径。

模型锁和推理 worker 从已安装包加载，不依赖源码目录。保留安装使用的 wheel 和 `python -m pip freeze` 输出，供升级或恢复时核对。

## 启动并验证服务

在运行目录执行以下步骤：

1. 加载服务配置：

   ```bash
   set -a
   . ./service.env
   set +a
   ```

2. 启动服务：

   ```bash
   python -m uvicorn framenote.service:app \
     --host 127.0.0.1 --port 18121 --workers 1 --no-proxy-headers
   ```

3. 在另一个终端检查进程活性：

   ```bash
   curl --fail http://127.0.0.1:18121/api/health
   ```

   响应中的 `status` 应为 `ok`；这项检查本身不代表 ASR 已就绪。

4. 检查 ASR 就绪状态：

   ```bash
   curl --fail http://127.0.0.1:18121/api/ready
   ```

   应返回 HTTP `200`，且 `configured`、`available` 均为 `true`。

5. 按[使用指南](USAGE.md#提交视频)提交一段有音轨的短视频，检查任务完成、转录可读且截图可取回。

用 Ctrl-C 或 SIGTERM 正常停止服务。运行中的任务标记为 `failed/interrupted`，排队任务在重启后继续，已落盘材料保留。需要重新处理时，重新提交视频。

## 配置 systemd 服务

本节用于 Ubuntu 首次安装。已有运行实例先按[版本发布与回滚](RELEASING.md)准备备份和候选环境。模板字段与配置文件须指向同一组路径：

| 用途 | 示例值 |
| --- | --- |
| 用户和组 | `framenote` |
| 工作目录 | `/opt/framenote/current`，指向 `releases/v0.2.1` |
| 虚拟环境 | `/opt/framenote/releases/v0.2.1/.venv` |
| 发布包 | `/opt/framenote/artifacts/v0.2.1` |
| 环境文件 | `/etc/framenote/service.env` |
| workspace | `/var/lib/framenote` |

先安装系统依赖，并把 wheel 放在服务用户可读取的位置，再完成以下步骤：

1. 如果专用用户尚不存在，创建用户：

   ```bash
   sudo useradd --system --home-dir /opt/framenote \
     --shell /usr/sbin/nologin framenote
   ```

2. 创建服务目录：

   ```bash
   sudo install -d -o framenote -g framenote \
     /opt/framenote /opt/framenote/releases /opt/framenote/releases/v0.2.1 \
     /opt/framenote/artifacts /opt/framenote/models /var/lib/framenote
   sudo install -d /etc/framenote
   ```

3. 为服务用户创建虚拟环境：

   ```bash
   sudo -u framenote python3.10 -m venv /opt/framenote/releases/v0.2.1/.venv
   ```

4. 安装 wheel：

   ```bash
   sudo -u framenote /opt/framenote/releases/v0.2.1/.venv/bin/python -m pip install \
     'WHEEL_PATH[asr]'
   ```

   将 `WHEEL_PATH` 替换为服务用户可读取的 wheel 绝对路径，保留 `[asr]`。

   按[制作发布包](RELEASING.md#制作发布包)归档本次 wheel、源码包和实际依赖清单，核验提交与版本。

5. 准备模型：

   ```bash
   sudo -u framenote /opt/framenote/releases/v0.2.1/.venv/bin/python -m framenote.models \
     prepare --out /opt/framenote/models
   ```

6. 核验模型：

   ```bash
   sudo -u framenote /opt/framenote/releases/v0.2.1/.venv/bin/python -m framenote.models \
     verify --out /opt/framenote/models
   ```

   应输出 `Verified all model assets`。

7. 将 `deploy/service.env.example` 复制到 `/etc/framenote/service.env`。
   如果目标文件已存在，保留原文件并核对配置。

8. 在环境文件中设置绝对路径，其他配置按需保留：

   ```text
   FRAMENOTE_WORKSPACE=/var/lib/framenote
   FRAMENOTE_SENSEVOICE_MODEL=/opt/framenote/models/sensevoice
   FRAMENOTE_VAD_MODEL=/opt/framenote/models/vad
   ```

   确认服务用户可读取模型和配置、可写 workspace。`EnvironmentFile` 的同名值覆盖 unit 中的 `Environment`，因此环境文件中的 workspace 必须与预期一致。

   首次安装时建立版本指针：

   ```bash
   sudo -u framenote ln -s releases/v0.2.1 /opt/framenote/current
   ```

   已有 `current` 或旧版服务时，使用[升级切换](RELEASING.md#升级切换)流程，保留原环境。

9. 从源码或源码归档根目录安装 unit：

   ```bash
   sudo cp deploy/framenote.service /etc/systemd/system/framenote.service
   ```

10. 检查 unit：

    ```bash
    sudo systemd-analyze verify /etc/systemd/system/framenote.service
    ```

11. 重新加载 systemd 配置：

    ```bash
    sudo systemctl daemon-reload
    ```

12. 启用并启动服务：

    ```bash
    sudo systemctl enable --now framenote.service
    ```

13. 检查服务状态：

    ```bash
    sudo systemctl status framenote.service --no-pager
    ```

    应显示服务运行中。失败时查看日志：

    ```bash
    sudo journalctl -u framenote.service -n 60 --no-pager
    ```

14. 按[启动并验证服务](#启动并验证服务)检查 `/api/ready` 并处理短视频。

模板监听 `0.0.0.0:18121`，使用单 worker、专用用户、工作目录和环境文件。`StateDirectory` 创建数据目录，失败后等待 5 秒重启。客户端配置填写这台机器可达的 HTTP 基址。

停止服务使用 `sudo systemctl stop framenote.service`，重启使用
`sudo systemctl restart framenote.service`。

### 停机行为

unit 使用 `KillMode=mixed`：systemd 先向主进程发送 SIGTERM，让服务停止队列并清理推理子进程；超过 `TimeoutStopSec=30` 后，才强制清理整个控制组。

保留这个设置。默认 `control-group` 可能先终止推理进程，导致正常停机被记为 ASR 失败，并在关停前启动下一项任务。相关真实检查见[生命周期验收](VALIDATION-NATIVE.md#生命周期与兼容)。

## 字幕与按需媒体的运行配置

B站默认优先读取平台字幕，无可用字幕时继续媒体获取和 ASR。服务仍按 `/api/ready` 检查 ASR 模型及依赖；字幕优先不替代模型准备。授权登录文件通过服务端本地 `python -m framenote.credentials import/status/clear` 管理，并用 `FRAMENOTE_BILIBILI_CREDENTIALS` 指向专用私有文件，详见[凭据管理](CONFIGURATION.md#b站私有凭据管理)。在 systemd 布局中可使用服务用户所有的 `/var/lib/framenote-private/`（0700）与其中的凭据文件（0600）；凭据与 `workspace/tasks/`、源码归档及客户端材料分开保管。

HTTP API 供受信访问；监听局域网时用防火墙限制调用者，公网经访问控制的代理或隧道接入。启用 Galaxy 会向第三方披露规范视频链接，配置前阅读[Galaxy 获取配置](CONFIGURATION.md#galaxy-获取配置)。`FRAMENOTE_PROCESS_TIMEOUT` 按阶段生效，字幕请求的重试和退避共享阶段截止时间；等待媒体操作的客户端超时不会取消服务端操作。

验收候选版本时使用独立 workspace 与端口（如 18122），先确认 `/api/ready=200`；检查字幕任务未提前获取媒体、分页全文与字幕原件哈希，再请求截图和有声视频，检查实际帧时间、音轨与缓存。旧版同步截图入口在媒体尚未准备时返回 `409/media_preparation_required`；升级客户端后使用后台媒体操作。

## 备份数据和环境

升级前，按以下步骤制作可恢复的备份：

1. 记录 wheel、源码提交、Python 版本及 `python -m pip freeze` 输出。
2. 停止服务，使 workspace 不再变化。
3. 备份服务配置、unit、虚拟环境和完整 workspace。
4. 在客户端备份笔记根目录，包括两份阅读稿和 `materials/`。
5. 在独立目录检查备份副本，核对转录与截图的 SHA-256、任务状态和笔记中的本地引用。

workspace 备份应包含已有源视频、独立音轨、`task.json`、转录、原始字幕、ASR 原件、候选尝试、`operations/`、媒体索引、截图和已落盘临时材料。字幕任务可以没有媒体。私有凭据单独按受限权限备份，不混入任务材料。systemd 示例中的备份目标为 `/var/lib/framenote`、`/etc/framenote`、`current` 实际指向的版本目录、对应发布包及 unit；记录符号链接目标，备份实际目录。模型可按锁文件重新下载，也可归档后用 `models verify` 核验。

虚拟环境脚本包含绝对路径；恢复环境归档时使用原机器的原路径。保留原目录，使用副本验证恢复过程。

## 升级服务

为新版本创建独立目录和虚拟环境，安装已归档 wheel，在独立 workspace / 端口完成候选验收。随后停服备份，切换 `current`，核对匹配配置 / 模型后启动，检查版本、就绪状态、旧材料和新任务。具体命令与首次迁移旧布局的恢复方式见[升级切换](RELEASING.md#升级切换)。旧环境保留原路径，不原地升级。

服务升级后，同步更新已安装的 `skills/framenote/` 完整目录（Skill 与 `scripts/headless.py` 一起更新），保留客户端 JSON 配置和现有笔记。新客户端可读取 v1/v2/v3 材料并恢复后台媒体等待；`--force-asr` 产生新任务时必须使用新的笔记目录，目录身份保护会拒绝覆盖旧字幕笔记。

升级期间保留模型与 workspace。记录安装版本和验证结果；如果检查失败，按[恢复旧版本](#恢复旧版本)恢复。

## 恢复旧版本

停服并保留当前数据及失败日志，核验旧发布包与环境后，先检查目标版本的数据兼容性，再切回旧目录和匹配配置。包含旧版不支持的 v3 材料时，用升级前备份创建恢复 workspace，保留升级后原目录；不能只切代码就假定任务兼容。完整步骤见[回滚与数据兼容](RELEASING.md#回滚与数据兼容)。

## 排障

根据状态或错误选择检查入口：

| 现象 | 检查与处理 |
| --- | --- |
| `/api/ready` 返回 `503`，提示缺少文件 | 对配置指向的模型目录执行 `prepare` 和 `verify`；核对 BPE、SenseVoice 和 VAD 配置文件 |
| ASR Python 或依赖不可用 | 用 `FRAMENOTE_ASR_PYTHON` 指向的解释器执行 `pip check`；确认 FrameNote 与 `[asr]` 已安装，修复后重启 |
| `asr_invalid_response` | 保留原始输出和任务目录的 `asr.log`；核对时间区间、模型版本和配置 |
| `asr_timeout` 或 `asr_failed` | 检查任务日志、超时设置、音轨和模型；修复后重新提交 |
| 地址不可达或端口冲突 | 核对 Uvicorn/unit 的地址和端口、进程状态及客户端服务地址 |
| `Workspace already in use` | 找出占用 workspace 的实例；为候选实例使用独立数据目录 |
| `failed/interrupted` | 读取保留材料，需要重新处理时重新提交；排队任务在重启后继续 |
| 客户端拒绝覆盖本地材料 | 对同一任务使用原目录；对新任务使用独立 `--out-dir` |

部署验收分别记录进程活性、ASR 就绪、真实推理和阅读结果。历史验收示例见[原生部署验收记录](VALIDATION-NATIVE.md)。
