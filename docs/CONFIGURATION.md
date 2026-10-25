# 配置参考

本页供部署者和客户端使用者查找 FrameNote 的配置项、默认值与覆盖规则。服务端使用环境变量；客户端使用 JSON 配置、环境变量和命令行参数。首次安装见[部署指南](DEPLOYMENT.md)，日常操作见[使用指南](USAGE.md)。

## 服务端配置

服务从进程环境读取配置。`service.env` 由 shell 或 systemd 的 `EnvironmentFile` 加载；服务不会自行读取这个文件。通过代码显式传给 `create_app()` 的 `Settings` 对象优先于环境变量。

路径先展开 `~`，相对路径按服务启动时的工作目录解析并固定。ASR Python 路径保留虚拟环境符号链接的身份。

服务支持以下环境变量：

| 环境变量 | 默认值 | 含义与约束 |
| --- | --- | --- |
| `FRAMENOTE_WORKSPACE` | `workspace` | 数据目录；任务保存在 `tasks/` 子目录 |
| `FRAMENOTE_BILIBILI_CREDENTIALS` | 未配置 | 服务端私有 B站凭据 JSON 路径；相对路径按服务启动目录解析 |
| `FRAMENOTE_ASR_BACKEND` | `sensevoice` | 后端名称；内置工厂登记 `sensevoice` |
| `FRAMENOTE_SENSEVOICE_MODEL` | `models/sensevoice` | SenseVoice 模型目录 |
| `FRAMENOTE_VAD_MODEL` | `models/vad` | 语音活动检测（VAD）模型目录 |
| `FRAMENOTE_ASR_PYTHON` | `sys.executable` | 执行推理的 Python；默认使用服务的虚拟环境 |
| `FRAMENOTE_ASR_THREADS` | `4` | 推理线程数，须为正整数 |
| `FRAMENOTE_PROCESS_TIMEOUT` | `7200` | 各受控阶段的超时秒数：字幕发现/正文共享该阶段 deadline，媒体进程、音频处理、封装、抽帧和 ASR 分别受限；不是整个任务总耗时上限，须为有限正数 |
| `FRAMENOTE_MAX_UPLOAD_BYTES` | `2147483648` | 上传上限，单位为字节，默认 2 GiB；须为正整数 |
| `FRAMENOTE_GALAXY_ENABLED` | `false` | `true`、`yes`、`1` 启用 Galaxy，不区分大小写；其他值关闭 |
| `FRAMENOTE_GALAXY_BASE_URL` | `https://downloader-api.bhwa233.com` | Galaxy API 基址 |

默认在同一虚拟环境安装服务与 `[asr]` 依赖。如果覆盖 `FRAMENOTE_ASR_PYTHON`，目标环境也必须安装 FrameNote 和 ASR 依赖。路径可以包含空格；在 shell 配置文件中用引号保护值，引号本身不属于变量值。

HTTP API 本身没有账号认证。限制服务为受信网络访问；公网使用有访问控制的反向代理或受保护隧道，避免未授权者提交任务、读取材料或触发媒体下载。MCP 保留 localhost Host/Origin 检查，这项检查不替代外部访问控制。

监听地址和端口由 Uvicorn 启动参数决定。同机访问使用 `--host 127.0.0.1`，局域网部署使用 `--host 0.0.0.0`；每个 workspace 只运行一个 worker。

## 加载配置并检查服务

准备好 `service.env`、依赖和模型后，在服务工作目录执行以下步骤：

1. 将配置导出到进程环境：

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

3. 在另一个终端检查就绪状态：

   ```bash
   curl --fail http://127.0.0.1:18121/api/ready
   ```

   模型文件完整且 ASR Python 依赖预检通过时，返回 HTTP `200`，`configured` 和 `available` 均为 `true`；否则返回 `503`，`reason` 说明缺项。

`/api/health` 返回进程活性、后端状态和 `galaxy_enabled`。其中 `asr_models_present` 表示必需模型文件存在，不表示哈希核验通过。

依赖预检在启动时执行并缓存。修复或安装 Python 依赖后，重启服务。健康请求复用缓存并检查模型文件是否存在；完整哈希核验使用[模型核验命令](MODELS.md#准备并核验模型)。

## B站私有凭据管理

凭据只在服务端本地管理。将授权登录流程取得的 JSON 导入专用私有目录；输入支持 `SESSDATA` / `sessdata`、`bili_jct`、`buvid3`、`buvid4`、`b_nut`、`b_lsid`，其他字段会忽略，控制字符会拒绝。输入文件也应妥善保管。

```bash
python -m framenote.credentials import --input /private/login.json --file /private/framenote/bilibili.json
python -m framenote.credentials status --file /private/framenote/bilibili.json
python -m framenote.credentials clear --file /private/framenote/bilibili.json
```

将示例路径换成服务用户可用的实际路径，在与服务相同的 `FRAMENOTE_WORKSPACE` 环境下运行。导入以原子替换写入 `0600` 文件，新建专用管理目录为 `0700`；已有父目录必须已是当前用户所有的 `0700` 目录，命令不会替共享目录修改权限。凭据路径须位于 `workspace/tasks/` 之外，不能经过符号链接；读取时拒绝组或其他用户可访问的文件。

`status` 仅返回 `configured`、`usable` 和各字段是否存在。`usable` 表示本地格式检查通过，不代表在线登录仍有效。`clear` 只删除指定凭据文件，保留目录与其他文件。缺失或失效凭据按匿名访问和后续获取回退处理，缺少凭据不会阻止服务启动。

在服务环境中配置路径：

```text
FRAMENOTE_BILIBILI_CREDENTIALS=/private/framenote/bilibili.json
```

修改配置路径后需重新加载服务环境。获取操作按路径读取凭据文件，原子导入更新文件后，下次读取即可使用新内容。凭据值不进入命令行参数、HTTP/MCP 参数、任务材料或日志。

账号 Cookie 仅发送给 `api.bilibili.com`、`www.bilibili.com`、`bilibili.com`、`passport.bilibili.com`，CDN 请求不带账号 Cookie。受控请求仅允许 HTTPS 的 `hdslb.com`、`bilibili.com`、`bilivideo.com`、`bilivideo.cn` 及其子域，每次重定向重新验证目标和 Cookie 范围，最多跟随 5 跳。请求适配器对临时网络错误最多尝试 3 次，单次最多 20 秒，退避 1 / 2 秒，并遵守阶段总截止时间和 `Retry-After`；认证失效直接分类，供获取流程回退。

## Galaxy 获取配置

Galaxy 使用 [galaxy-downloader](https://github.com/bhwa233/galaxy-downloader) 项目的后端 API。获取路径取决于总开关：

| 平台 | `false` | `true` |
| --- | --- | --- |
| 抖音 | 使用平台下载器 | 直接使用 Galaxy API |
| B站、YouTube | 使用平台下载器 | 先使用平台下载器，失败后尝试 Galaxy |

要启用 Galaxy，在 `service.env` 中设置：

```text
FRAMENOTE_GALAXY_ENABLED=true
```

重新加载配置并重启服务后，检查 `/api/health` 中的 `galaxy_enabled=true`。Galaxy 获取失败时，当前获取阶段失败；已完成的转录保留。启用后，目标视频的规范链接（包括 B站分 P）会发送给配置的第三方 Galaxy 服务；账号 Cookie 不发送给 Galaxy。请在受信且接受该链接披露的环境中启用。

服务用 `ffmpeg` 下载并封装 HTTP Live Streaming（HLS）播放列表，用 `curl` 下载直链媒体，再根据 `ffprobe` 检测到的轨道判断是否保留独立音频。已完成素材会保留，失败或取消的临时下载会清理。解析响应中的临时媒体地址不写入公开元数据；服务机器仍需能访问返回的媒体 CDN。

## 客户端配置

`headless.py` 在 Agent 所在机器运行，独立读取客户端配置。用户级文件为 `~/.config/framenote/client.json`，项目级文件为 `.framenote.json`，两者使用相同格式。

客户端接受以下字段：

| 字段 | 默认值 | 含义与约束 |
| --- | --- | --- |
| `service_url` | `http://127.0.0.1:18121` | 非空 HTTP 或 HTTPS 基址，不含凭据、查询参数或片段 |
| `notes_dir` | 按工作目录推导 | 笔记父目录；支持 `~`，相对路径按配置文件所在目录解析 |
| `download_video` | `false` | 是否自动取回视频，须为 JSON 布尔值 |

例如，在工作项目中创建 `.framenote.json`：

```json
{
  "service_url": "http://SERVER:18121",
  "notes_dir": "./video-notes",
  "download_video": false
}
```

将 `SERVER` 替换为客户端可达的服务器主机名或 IP 地址。该配置把笔记父目录设为配置文件旁的 `video-notes/`。未知字段、类型错误和无效 JSON 都会报错。

配置逐字段覆盖，优先级从高到低为：

1. 命令行参数。
2. 环境变量。
3. 最近的项目配置。
4. 用户级配置。
5. 内置默认值。

脚本从执行目录向上查找最近一份 `.framenote.json`，查到 Git 根目录后停止，包括使用 `.git` 文件的 worktree。非 Git 目录查到文件系统根。脚本只读取最近一份项目配置；未填写的字段继承用户配置。Skill 安装位置不参与查找。

命令行与环境变量的对应关系如下：

| 字段 | 命令行参数 | 环境变量 |
| --- | --- | --- |
| `service_url` | `--service-url` | `FRAMENOTE_SERVICE_URL` |
| `notes_dir` | `--notes-dir` | `FRAMENOTE_NOTES_DIR` |
| `download_video` | `--download-video`、`--no-download-video` | `FRAMENOTE_DOWNLOAD_VIDEO` |

命令行参数可位于子命令前后。`FRAMENOTE_DOWNLOAD_VIDEO` 接受 `true/false`、`yes/no`、`1/0`、`on/off`，不区分大小写，并忽略两端空白。命令行或环境变量中的相对目录按执行目录解析。

## 检查配置和笔记目录

在你要保存笔记的工作项目中执行以下步骤：

1. 查看生效配置：

   ```bash
   python3 -S 'SKILL_DIR/scripts/headless.py' config
   ```

   将 `SKILL_DIR` 替换为已安装 Skill 的绝对目录。输出包含配置值、项目和用户配置路径，以及 `sources` 中各字段的来源。

2. 查看笔记父目录：

   ```bash
   python3 -S 'SKILL_DIR/scripts/headless.py' notes-dir
   ```

   使用同一个 `SKILL_DIR`。确认输出中的 `notes_directory` 为预期目录。

未配置 `notes_dir` 时，脚本按以下规则选择目录：

- 在 FrameNote 项目中，使用项目根目录的 `workspace/notes/`。
- 在其他 Git 项目中，使用仓库根目录的 `framenote-notes/`。
- 在非 Git 目录中，使用执行目录下的 `framenote-notes/`。

`notes_dir` 是供工作流选择保存位置的父目录。下载命令仍须用 `--out-dir` 显式指定本次任务的笔记根目录；材料写入它的 `materials/` 子目录。

## 视频取回配置

设置 `download_video: true` 或 `FRAMENOTE_DOWNLOAD_VIDEO=true` 后，客户端在取回材料时自动下载视频。`--download-video` 和 `--no-download-video` 覆盖单次执行。

当前客户端会先创建后台媒体操作，再等待和下载；等待超时会保留 `operation_id`，可用 `operation TASK_ID OPERATION_ID --wait 45` 继续查询，或 `cancel-operation TASK_ID OPERATION_ID` 取消。超时不等于后台操作已停止。

服务在视频可用时通过任务状态和材料清单返回 `downloads.video`，形式为 `/api/task/TASK_ID/video`，其中 `TASK_ID` 是实际任务 ID。客户端使用该入口，不依赖服务端绝对路径。HTTP GET 流式返回视频并支持 ETag 条件请求。

下载位置、失败重试和文件复用规则见[取回视频](USAGE.md#取回视频)。配置完成后，按[提交视频](USAGE.md#提交视频)验证一次完整流程。
