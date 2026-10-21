# 获取视频材料并整理笔记

本指南面向已连接 FrameNote 服务的使用者和 Agent，介绍如何提交视频、读取转录、抽取截图、取回视频和继续已有任务。完成后，你可以把材料交给 [FrameNote Skill](../skills/framenote/SKILL.md) 整理为章节笔记。

## 开始之前

确认以下条件已满足：

- 已按[README](../README.md)启动服务，`/api/ready` 返回 `200`。
- 客户端机器有 Python 3.10–3.12，并保存了完整的 `skills/framenote/` 目录。
- 已在工作项目中配置服务地址；配置规则见[客户端配置](CONFIGURATION.md#客户端配置)。

本页示例在 FrameNote 仓库根目录执行。其他安装方式请把
`skills/framenote/scripts/headless.py` 替换为脚本的绝对路径，同时保持工作项目目录不变。

## 提交视频

每个任务使用独立的笔记根目录。按以下步骤获取一份材料：

1. 提交视频并等待处理：

   ```bash
   python3 -S skills/framenote/scripts/headless.py transcribe 'VIDEO_SOURCE' \
     --out-dir ./my-note --wait 1800
   ```

   将 `VIDEO_SOURCE` 替换为平台链接或本地视频路径。客户端输出任务 ID 和进度；本地文件通过流式上传发送到服务。默认上传上限为 2 GiB，可通过服务配置调整。

2. 检查 `my-note/materials/bundle.json`。
   成功时，`task.status` 为 `completed`，`download_complete` 为 `true`，转录文件位于同一 `materials/` 目录。

3. 读取 `materials/metadata.json` 和 `materials/transcript.json`，确认来源和转录内容对应目标视频。

`--wait` 只控制客户端等待时间。等待超时后，服务继续处理；保留输出中的任务 ID，按[继续已有任务](#继续已有任务)取回结果。

支持的输入及处理方式如下：

| 输入 | 处理方式 |
| --- | --- |
| B站 | 处理单个视频或 `?p=N` 指定的分 P；默认第 1 P |
| 抖音 | 处理单个视频；启用 Galaxy 时直接通过 Galaxy API 获取 |
| YouTube | 支持普通视频、短链、Shorts、嵌入和已结束直播的视频链接；含播放列表参数时只处理当前视频 |
| 本地视频 | 上传视频文件，服务保留源文件供后续抽帧和取回 |

Galaxy 开启时，B站和 YouTube 优先使用平台下载器，失败后尝试 Galaxy；关闭时各平台使用原生下载。来源选择和日志说明见[Galaxy 获取配置](CONFIGURATION.md#galaxy-获取配置)。

## 继续已有任务

从命令输出或 `materials/bundle.json` 获取任务 ID。后续示例中的 `TASK_ID` 均替换为这个 ID，并继续使用同一笔记根目录。

1. 检查任务状态：

   ```bash
   python3 -S skills/framenote/scripts/headless.py status TASK_ID
   ```

2. 如果任务仍在排队或运行，等待最多 45 秒：

   ```bash
   python3 -S skills/framenote/scripts/headless.py wait TASK_ID --timeout 45
   ```

   查看返回的 `status`。`wait` 返回状态，不下载材料；等待结束也不等于任务已完成。

3. 任务完成后，取回材料：

   ```bash
   python3 -S skills/framenote/scripts/headless.py fetch TASK_ID \
     --out-dir ./my-note
   ```

   检查 `bundle.json` 中的 `download_complete`。失败或取消的任务也可以用 `fetch` 取回已经生成的材料。

如需查找任务，使用 `tasks --offset 0 --limit 20`，增加 `offset` 翻页。任务列表每页最多 100 项。取消任务使用 `cancel TASK_ID`；服务等待进程组清理后再处理下一项。

排队任务在服务重启后继续；运行中被中断的任务标记为 `failed`，`phase` 和错误码为 `interrupted`，已落盘材料保留。重新处理时提交一个新任务。

## 分页读取转录

完整转录在 `materials/transcript.json`。如果只需读取一段时间或分批读取长视频，可调用转录接口：

```bash
python3 -S skills/framenote/scripts/headless.py transcript TASK_ID \
  --start 30 --end 90 --offset 0 --limit 200
```

时间单位为秒。接口选取与时间区间重叠的段落，再按 `offset` 和 `limit` 分页；`total_segments` 是时间筛选后的总段数，`text` 只包含本页文本。每页最多 500 段。

要阅读整个视频，省略 `--end`，从 `--start 0 --offset 0` 开始，逐页增加 `offset`，直到覆盖 `total_segments`。`--output-format markdown` 或 `--output-format srt` 在 JSON 响应的 `content` 中返回相应格式。

SenseVoice 的 `timestamp_kind` 为 `VAD_interval`，表示语音活动检测（VAD）区间，每段最长 30 秒。章节边界应表达为近似时间范围。

## 抽取章节截图

阅读转录并确定章节后，按以下步骤选择画面：

1. 提交截图时间点：

   ```bash
   python3 -S skills/framenote/scripts/headless.py frames TASK_ID \
     --timestamps 30 90 180 --out-dir ./my-note
   ```

   把示例时间替换为目标章节的时间，单位为秒。每次接受 1–24 个时间点，每个值须大于或等于 `0` 且小于视频时长。

2. 查看 `materials/frames.json` 中的截图时间和 `relative_path`。
   路径相对笔记根目录，例如 `materials/frames/FRAME_ID.jpg`，其中 `FRAME_ID` 为服务生成的截图 ID。

3. 打开实际截图，确认画面后再写图注，并在笔记中标注索引记录的截图时间。

命令自动下载截图及材料索引。追加截图后，原有截图仍保留在索引中。

## 取回视频

视频自动取回默认关闭。要为某个任务取回服务端保留的视频，运行：

```bash
python3 -S skills/framenote/scripts/headless.py fetch TASK_ID \
  --out-dir ./my-note --download-video
```

视频保存为 `materials/video.*`，实际扩展名取决于媒体格式。`bundle.json` 记录本地路径、字节数、SHA-256 和 ETag。检查 `download_complete=true` 及 `video` 记录，确认材料完整。

视频沿用服务已获取的画质。视频与音频分离时，服务按需合并为缓存 MP4，保留源素材。重复取回时，客户端先校验本地大小和哈希，再通过 ETag 条件请求复用未变化的文件。

下载失败时，客户端清理临时 `.part` 文件、保留已有完整文件，并保持 `download_complete=false`。重试同一条 `fetch` 命令即可。关闭自动取回会保留已下载的视频。全局设置和单次覆盖见[视频取回配置](CONFIGURATION.md#视频取回配置)。

## 阅读和保留材料

完成 Skill 写作流程后的目录结构如下：

```text
my-note/
├── notes.md                    # 章节笔记与截图
├── chapters-transcript.md      # 分章原稿
└── materials/
    ├── frames/                 # 截图文件
    ├── frames.json             # 截图时间与路径索引
    ├── transcript.json         # 原始转录与时间
    ├── transcript.md           # 连续原稿
    ├── transcript.srt          # 字幕
    ├── asr.raw.json            # ASR 原始输出
    ├── metadata.json           # 来源信息
    ├── bundle.json             # 任务身份与下载结果
    ├── video.*                 # 开启视频取回时生成
    └── corrections.md          # 有纠错或疑点时生成
```

`--out-dir` 始终表示笔记根目录。重取材料保留两份阅读文档、`corrections.md` 和已保存的视频；不同任务或不同服务不能复用已标记的材料目录。旧版根目录布局需要先整理到 `materials/`，或改用一个新目录。

## 客户端命令参考

所有子命令接受 `--service-url`、`--notes-dir`、`--download-video` 和 `--no-download-video`；参数可放在子命令前后。完整调用格式可用 `--help` 查看。

各命令的用途和专用参数如下。`TASK_ID` 表示任务 ID，`SOURCE` 表示链接或本地路径，`DIR` 表示笔记根目录，`SECONDS` 表示秒数，`N` 表示整数：

| 命令 | 参数 | 结果 |
| --- | --- | --- |
| `config` | 无专用参数 | 显示生效配置及字段来源 |
| `notes-dir` | 无专用参数 | 显示笔记父目录 |
| `health` | 无专用参数 | 显示服务活性和后端状态 |
| `transcribe SOURCE` | `--out-dir DIR` 必填；`--wait SECONDS` 默认 `1800` | 提交、等待并取回已生成材料 |
| `tasks` | `--offset N` 默认 `0`；`--limit N` 默认 `20`，最大 `100` | 分页列出任务 |
| `status TASK_ID` | 无专用参数 | 返回任务状态 |
| `wait TASK_ID` | `--timeout SECONDS` 默认 `45` | 等待终态或超时，返回状态 |
| `cancel TASK_ID` | 无专用参数 | 请求取消任务 |
| `transcript TASK_ID` | `--start SECONDS` 默认 `0`；`--end SECONDS` 可省略；`--offset N` 默认 `0`；`--limit N` 默认 `200`，最大 `500`；`--output-format` 为 `json`、`markdown` 或 `srt`，默认 `json` | 返回筛选后的转录页 |
| `frames TASK_ID` | `--timestamps SECONDS ...` 和 `--out-dir DIR` 必填 | 抽帧并取回材料 |
| `artifacts TASK_ID` | 无专用参数 | 列出可下载材料和视频入口 |
| `fetch TASK_ID` | `--out-dir DIR` 必填 | 取回已生成材料 |

完成材料获取后，按[Skill 写作流程](../skills/framenote/SKILL.md)生成并检查两份阅读文档。
