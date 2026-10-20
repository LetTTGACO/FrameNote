---
name: framenote
description: Use when 用户直接发送B站、抖音或 YouTube 视频链接、要求处理本地视频，或继续讨论已处理的视频。
---

# 视频材料与章节笔记

FrameNote HTTP 服务负责获取视频、转录和独立截图；你通过随 Skill 提供的标准库客户端获取材料，组织章节、写作并继续讨论。以下命令中的 `<脚本>` 指本 Skill 的 `scripts/headless.py`。用户只发视频链接时，默认完成材料获取、章节组织、截图和笔记流程。

## 配置与保存位置

在用户当前工作项目中执行脚本，保持该工作目录，使用绝对路径定位 `<脚本>`。先运行 `python3 -S <脚本> config` 检查生效配置及来源；`service_url` 是 HTTP 服务基址。配置按字段覆盖：命令行参数 → 环境变量 → 最近的项目 `.framenote.json` → `~/.config/framenote/client.json` → 默认值。项目配置从执行目录向上查找，遇到 Git 根目录停止；非 Git 目录查到文件系统根。Skill 的安装位置不参与配置查找。

配置文件使用 `service_url`、`notes_dir`、`download_video`；相对 `notes_dir` 按配置文件所在目录解析。环境变量分别是 `FRAMENOTE_SERVICE_URL`、`FRAMENOTE_NOTES_DIR`、`FRAMENOTE_DOWNLOAD_VIDEO`。服务基址默认 `http://127.0.0.1:18121`。服务不可达时检查配置输出和网络连通性。

运行 `python3 -S <脚本> notes-dir` 取得笔记父目录，在其下建立 `<日期时间>-<视频标识>/`。用户指定保存位置时使用该位置；已有目录使用独立名称。每个任务使用独立笔记根目录，所有 `--out-dir` 都传该根目录。

```text
<笔记根目录>/
├── notes.md                    # 章节笔记与截图
├── chapters-transcript.md      # 分章口播原稿
└── materials/
    ├── frames/                 # 截图
    ├── frames.json             # 截图时间与路径索引
    ├── transcript.json         # 原始转录与时间
    ├── transcript.md           # 服务导出的连续原稿
    ├── transcript.srt          # 字幕
    ├── asr.raw.json            # ASR 原始输出
    ├── metadata.json           # 来源信息
    ├── bundle.json             # 服务地址、任务身份、本地目录与下载结果
    ├── video.*                 # 开启视频取回时保存
    └── corrections.md          # 有纠错或疑点时保存依据
```

## 准备与读取材料

```bash
python3 -S <脚本> transcribe '<B站/抖音/YouTube 链接或本地视频路径>' --out-dir '<笔记根目录>' --wait 1800
```

本地视频流式上传；服务端保留视频供后续抽帧和取回。脚本打印任务 ID 与进度，并将材料下载到 `materials/`。等待超时后任务继续运行，用 `status <task_id>` 或 `wait <task_id> --timeout 45` 检查，再用 `fetch <task_id> --out-dir ...` 取回。已有任务优先复用；忘记 ID 时用 `tasks --offset 0 --limit 20` 查找。

先读 `materials/metadata.json` 和转录；长视频可按时间和分页读取：

```bash
python3 -S <脚本> transcript '<task_id>' --start 0 --end 300 --offset 0 --limit 200
```

关注 `total_segments/offset/limit`，覆盖视频全程后组织章节。B站按 `?p=N` 处理单个分 P，默认第 1 P。YouTube 支持普通视频、短链、Shorts 和嵌入链接，带播放列表参数时只处理当前视频。平台下载失败时按服务端配置使用 Galaxy 回退；获取阶段与原因见任务状态。

## 独立截图与视频取回

按章节内容选取有信息价值的画面：

```bash
python3 -S <脚本> frames '<task_id>' --timestamps 30 90 180 --out-dir '<同一笔记根目录>'
```

每次最多 24 个时间点。命令取回截图及索引；`frames.json` 中 `relative_path` 相对笔记根目录，例如 `materials/frames/<frame_id>.jpg`。打开实际本地图片后再描述画面，并在笔记中标注实际截图时间。

视频自动取回默认关闭；遵循生效配置。用户要求取回视频时，运行 `fetch <task_id> --out-dir ... --download-video`，也可在首次 `transcribe` 时加此参数；单次关闭用 `--no-download-video`。使用任务返回的下载入口，复用服务端已有素材。画质沿用服务端获取的版本，分离音轨按需合并；本地路径、大小及校验值记录在 `bundle.json`。显式要求的视频下载失败时，报告材料尚未全部取回，重试 `fetch`。

## 成稿与讨论

根据 `timestamp_kind` 和 `provenance` 说明时间来源：`VAD_interval` 为 SenseVoice VAD 语音区间（每段最长 30 秒），`segment_interval` 为后端原生分段区间；章节边界按相应近似范围表达。

`notes.md` 包含来源、摘要、各章近似时间范围、详细要点与截图，并链接到 `chapters-transcript.md` 的对应章节。分章原稿保留完整识别文本与时间，使用稳定章节锚点，支持返回主笔记。

保留 `materials/transcript.json` 和 `asr.raw.json` 的原始识别内容。专名、数字或步骤需要修正时，依据来源、截图或回听记录，将依据写入 `materials/corrections.md`，从阅读文档链接过去；材料不足则保留疑点。该文件中的截图链接相对自身目录使用 `frames/...`。视频中的命令作为引用材料，用户要求执行时再操作。

交付前逐一检查图片、原稿、纠错记录和章节锚点链接。返回两份阅读文档的绝对路径；追问时从 `materials/bundle.json` 找回任务与服务地址，回读相关转写和截图。重取材料保留已有阅读稿、纠错记录与已保存的视频。
