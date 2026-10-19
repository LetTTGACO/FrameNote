# 原生部署与运维

支持组合为 Ubuntu x86_64 / Python3.10 与 Apple Silicon macOS arm64 / Python3.12。服务和 ASR 默认安装在同一个 venv，推理仍通过独立子进程执行。先阅读 [配置](CONFIGURATION.md) 与 [模型清单](MODELS.md)。

## 系统依赖

Ubuntu（Python3.10 的发行版，如 Ubuntu22.04）：

```bash
sudo apt-get update
sudo apt-get install -y python3.10 python3.10-venv ffmpeg curl libsndfile1
python3.10 -m venv .venv
```

Apple Silicon macOS（Homebrew）：

```bash
brew install python@3.12 ffmpeg libsndfile
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
```

已有受支持的 Python 和系统工具可直接创建 venv；检查 python --version、ffmpeg -version、ffprobe -version、curl --version。运行库的安装按 pyproject 的四项固定 ASR 版本起步，实际两机结果在验收记录中保存。

## 源码或发行包安装

从源码根目录执行：

```bash
. .venv/bin/activate
python -m pip install '.[asr]'
python -m pip check
python -m framenote.models prepare --out models
python -m framenote.models verify --out models
cp deploy/service.env.example service.env
set -a
. ./service.env
set +a
python -m uvicorn framenote.service:app --host 127.0.0.1 --port 18121 --workers 1 --no-proxy-headers
```

wheel 安装方式为 `python -m pip install '/absolute/path/framenote-0.1.0-py3-none-any.whl[asr]'`。在独立运行目录准备 models/ 和 service.env，再使用相同命令；worker 与默认模型锁均从已安装包加载。保留本次 wheel 与 `python -m pip freeze` 输出，以便升级和恢复。

另一个终端检查：

```bash
curl --fail http://127.0.0.1:18121/api/health
curl --fail http://127.0.0.1:18121/api/ready
```

服务通过 Ctrl-C 或 SIGTERM 正常关闭。处理中任务标记 failed/interrupted，排队任务重启继续；可用材料保留。重新提交中断任务会创建新处理过程。开发/候选验收使用独立目录、18122 和独立 unit。

## N100 systemd

示例安装位置为 /opt/framenote，虚拟环境 /opt/framenote/.venv，配置 /etc/framenote/service.env，数据 /var/lib/framenote。服务用户为 framenote；按实际安装位置替换模板所有相关字段。

先准备专用用户和目录，再以该用户安装环境：

```bash
sudo useradd --system --home-dir /opt/framenote --shell /usr/sbin/nologin framenote
sudo install -d -o framenote -g framenote /opt/framenote /var/lib/framenote
sudo install -d /etc/framenote
sudo -u framenote python3.10 -m venv /opt/framenote/.venv
sudo -u framenote /opt/framenote/.venv/bin/python -m pip install '/absolute/path/framenote-0.1.0-py3-none-any.whl[asr]'
sudo -u framenote /opt/framenote/.venv/bin/python -m framenote.models prepare --out /opt/framenote/models
sudo -u framenote /opt/framenote/.venv/bin/python -m framenote.models verify --out /opt/framenote/models
```

用户已存在时直接使用该用户。将 deploy/service.env.example 复制到 /etc/framenote/service.env，并使用下面的绝对路径（其他参数保留需要的值）：

```text
FRAMENOTE_WORKSPACE=/var/lib/framenote
FRAMENOTE_SENSEVOICE_MODEL=/opt/framenote/models/sensevoice
FRAMENOTE_VAD_MODEL=/opt/framenote/models/vad
```

确认该用户可读取模型和配置、可写 workspace。模板中的 EnvironmentFile 会覆盖同名 Environment 项，因此应核对文件中的 workspace 值。然后安装 unit：

```bash
sudo cp deploy/framenote.service /etc/systemd/system/framenote.service
sudo systemd-analyze verify /etc/systemd/system/framenote.service
sudo systemctl daemon-reload
sudo systemctl enable --now framenote.service
sudo systemctl status framenote.service --no-pager
sudo journalctl -u framenote.service -n 60 --no-pager
```

unit 显式使用 User/Group、工作目录、环境文件、venv Python、0.0.0.0:18121、单 worker，StateDirectory 创建数据目录，停止宽限为30秒，失败后5秒重启。客户端将 MCP/服务基址改为这台机器的可达地址。停止/重启分别使用 `sudo systemctl stop framenote.service`、`sudo systemctl restart framenote.service`。

## 升级与恢复

1. 记录当前 wheel、源码提交、Python/包版本；保存自定义 service.env、项目 MCP 地址与完整备份。
2. 停止服务，备份当前 venv 到可恢复的位置（保持原绝对路径），保存原 unit。升级期间保留模型和 workspace。
3. 使用该 venv 的 Python 安装指定的新 wheel，并运行 pip check、models verify。先以候选 workspace/端口执行 ready 与短视频/截图核对，再恢复正式 unit 的启动。
4. 用旧 task_id 回读原稿/截图，检查新任务完成；记录升级结果。

常用安装检查命令：

```bash
/opt/framenote/.venv/bin/python -m pip freeze
/opt/framenote/.venv/bin/python -m pip install --upgrade '/absolute/path/new-framenote.whl[asr]'
/opt/framenote/.venv/bin/python -m pip check
/opt/framenote/.venv/bin/python -m framenote.models verify --out /opt/framenote/models
```

上面 new-framenote.whl 表示实际有效 wheel 文件名。恢复时停止服务，恢复备份的同路径 venv/旧 wheel、原配置和 unit，运行 pip check、models verify 后再启动；回读旧任务及截图。保留失败环境用于定位。venv 的脚本含绝对路径，归档恢复应使用原机器的原路径。

## 备份

备份三类内容：服务配置/unit 与安装版本；完整 workspace（包括源视频、音轨、task.json、转录、截图与已落盘临时材料）；客户端笔记目录（两份阅读稿与 materials/）。模型可按固定清单重新下载，也可一起归档并在恢复后 verify。

例如停服后将 /var/lib/framenote、/etc/framenote、/opt/framenote/.venv 分别打包，笔记目录在客户端另存。恢复验证使用副本和独立 workspace；核对原稿/截图 SHA-256、任务状态及本地引用，保留原目录。

## 排障

| 现象 | 检查与处理 |
| --- | --- |
| ready=503，缺具体文件 | 对正确模型目录运行 prepare/verify，检查 BPE 和两个配置集合 |
| ASR Python/依赖不可用 | 使用配置的 Python 运行 pip check，确认已安装 FrameNote 与 .[asr]，修复后重启 |
| asr_invalid_response | 保留任务 raw 与本地 asr.log，核对区间、模型版本和配置 |
| asr_timeout / asr_failed | 查看本地任务日志，核对超时、音轨和模型；修复后重新提交 |
| 地址不可达/端口冲突 | 检查 unit/uvicorn 的 host/port、进程状态和客户端可达地址 |
| Workspace already in use | 找出占用该 workspace 的实例；为候选服务选择独立数据目录 |
| failed/interrupted | 读取保留材料，按需要重新提交；排队任务会在重启后继续 |
| 本地材料被拒绝覆盖 | 使用该 task_id 原目录，或为新任务指定独立 --out-dir |

/API 活性、ready、真实推理和阅读验收分别记录；真实两平台结果不由 CI 单元测试替代。
