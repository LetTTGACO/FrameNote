---
name: framenote
description: 用户直接发送B站、抖音视频链接或要求处理本地视频时，调用Homelab服务转录和抽帧，生成带章节截图、时间范围和口播原稿的结构化笔记；也用于继续讨论已处理的视频。
---

# 视频材料与章节笔记

服务在 Homelab 准备视频、SenseVoiceSmall 转录和截图；你根据材料写作并继续讨论。服务地址使用 `FRAMENOTE_SERVICE_URL` 或脚本的 `--service-url`，默认 `http://192.168.31.56:18121`。Mac直接通过局域网访问该地址。脚本仅需 Python 标准库。

用户只发视频链接时，默认执行完整的材料获取、章节组织、截图和笔记流程。未指定保存位置时，解析本技能目录的符号链接，在所属 `FrameNote` 仓库的 `workspace/notes/<日期>-<视频标识>/` 保存 `notes.md` 和材料；同一视频的新任务使用独立子目录。

## 保存结构

每份笔记的顶层保留两份阅读文档，截图、原始材料及纠错记录归入 `materials/`：

```text
<笔记根目录>/
├── notes.md                    # 章节笔记与截图
├── chapters-transcript.md      # 分章口播原稿
└── materials/
    ├── frames/                # 截图
    ├── frames.json            # 截图时间与路径索引
    ├── transcript.json         # 原始转录与时间
    ├── transcript.md           # 服务导出的连续原稿
    ├── transcript.srt          # 字幕
    ├── asr.raw.json            # ASR 原始输出
    ├── metadata.json           # 视频来源信息
    ├── bundle.json             # 任务身份与本地目录
    └── corrections.md          # 有纠错或疑点时保存依据
```

所有脚本命令的 `--out-dir` 都传笔记根目录，脚本会将下载材料写入其中的 `materials/`。`frames.json` 的 `relative_path` 相对笔记根目录，例如 `materials/frames/<frame_id>.jpg`。

## 准备材料

优先使用已配置的 `framenote` MCP：`transcribe_video` → `wait_for_task` → `get_transcript`。每次等待最多45秒；转录支持按时间范围和分页读取，关注 `total_segments/offset/limit`，确保覆盖视频全程。MCP运行在Homelab，Streamable HTTP地址为 `http://192.168.31.56:18121/mcp`。

本地视频上传或MCP工具尚未加载时，使用脚本：

```bash
python3 <本技能目录>/scripts/headless.py --service-url http://192.168.31.56:18121 \
  transcribe '<B站/抖音链接或本地视频绝对路径>' --out-dir '<笔记根目录>' --wait 1800
```

本地文件会流式上传；服务保留原视频供后续截图。每个任务使用独立笔记根目录，同一task_id可反复取回材料或追加截图。脚本打印任务ID和进度，并将转录、原始ASR结果、来源信息下载到根目录下的 `materials/`。等待超时后任务仍在服务端继续，可用 `status <task_id>` 查看，再用 `fetch <task_id> --out-dir ...` 取回材料。服务不可达时检查已配置的服务地址和局域网连通性。

MCP任务完成后，用 `python3 <本技能目录>/scripts/headless.py fetch '<task_id>' --out-dir '<笔记根目录>'` 将转录与来源材料取回Mac。

B站按链接 `?p=N` 处理单个分P，未指定时处理第1P。抖音先尝试本地下载器，再按服务端配置使用 Galaxy 外部 API；获取失败的阶段与原因在任务状态中。已有任务可直接回读。

## 阅读与选图

先读 `materials/metadata.json`、`materials/transcript.json`，按语义规划章节。SenseVoice 时间来自 **VAD语音片段区间**，每段最长30秒；章节边界按近似范围表达。原始输出保存在 `materials/asr.raw.json`，原稿尚未人工校对。

按各章内容选择有信息价值的画面时间点，独立请求截图：

```bash
python3 <本技能目录>/scripts/headless.py --service-url http://192.168.31.56:18121 \
  frames '<task_id>' --timestamps 30 90 180 --out-dir '<同一笔记根目录>'
```

每次最多24个时间点。`materials/frames.json` 保存请求时间、实际帧时间和本地 `relative_path`；读取实际图片后再描述画面。MCP 的 `extract_frames` 返回索引，`get_frame` 返回原生图像，抽帧后再用 `fetch` 下载新增图片。`notes.md` 使用 `materials/frames/...` 引用图片，并标注实际截图时间。

## 成稿与讨论

按用户需求选择详略；`notes.md` 包含来源与摘要，各章含近似时间范围、详细要点、相关截图，并链接到 `chapters-transcript.md` 的对应章节。分章原稿保留完整识别文本与时间，使用稳定的章节锚点，支持返回主笔记。按范围阅读到结尾，再完成章节组织。

保持 `materials/transcript.json`、`materials/asr.raw.json` 的原始识别内容。专名、数字或步骤如需修正，依据标题、简介、截图或回听记录，将依据保存为 `materials/corrections.md`，从阅读文档链接过去。该文件中的截图链接相对其自身目录使用 `frames/...`。材料不足的项目保留疑点；视频中的命令作为引用材料，用户要求执行时再操作。

将两份阅读文档保存到笔记根目录，记录 task_id，材料索引指向 `materials/` 中的对应文件。交付前逐一检查图片、原稿、纠错记录和章节锚点链接，向用户返回可打开的 `notes.md` 绝对路径。追问时优先回读相关时间范围及截图，复用已有任务。
