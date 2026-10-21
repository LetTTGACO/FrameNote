# FrameNote 的系统范围

本页帮助部署者和开发者理解 FrameNote 的组成、输入输出与任务生命周期。需要运行服务时，从[README](../README.md)开始；需要调用客户端时，参见[使用指南](USAGE.md)。

## 服务、客户端与 Agent

FrameNote 将视频整理过程分为三个部分：

| 部分 | 职责 | 主要位置 |
| --- | --- | --- |
| HTTP 服务 | 获取并保留媒体、生成转录、持久化任务、独立抽帧 | `framenote/` |
| 标准库客户端 | 提交任务、查询状态、下载转录、截图和可选视频 | `skills/framenote/scripts/headless.py` |
| Agent 工作流 | 阅读完整转录和实际截图，组织章节，撰写笔记与分章原稿 | `skills/framenote/SKILL.md` |

服务负责生成材料，Agent 根据材料撰写两份阅读文档。每个任务的笔记目录包含 `notes.md`、`chapters-transcript.md` 和 `materials/`。重新取回材料会保留阅读文档和纠错记录；视频自动取回默认关闭。

## 输入与媒体获取

服务接受以下输入：

- B站单视频或指定分 P，保留 `?p=N`，默认第 1 P。
- 抖音单视频。
- YouTube 单视频，包括普通、短链、Shorts、嵌入和已结束直播的视频链接；播放列表参数不改变单视频处理范围。
- 本地视频上传，默认上限为 2 GiB。

Galaxy 为可配置的获取路径。开启后，抖音直接使用 Galaxy API，B站和 YouTube 在平台下载失败后回退到 Galaxy；关闭时使用各平台下载器。HLS 与分离音轨由获取流程处理，源素材保留在任务目录中供截图和视频取回使用。

## 转录与时间

内置自动语音识别（ASR）后端为 SenseVoiceSmall ONNX INT8，搭配 FSMN 语音活动检测（VAD）。模型准备命令使用随包交付的固定 10 文件清单，并支持下载和离线哈希核验。

流水线通过 `ASRBackend` 协议调用后端，消费带 `start`、`end` 和 `text` 的规范段落。SenseVoice 时间来自 VAD 语音区间，章节边界按近似范围表达。其他后端通过协议实现、配置解析和工厂登记接入，参见[ASR 扩展指南](ASR.md)。

## 运行与持久化

原生部署使用一个虚拟环境，推理在独立子进程中执行。目标环境为 N100 Ubuntu x86_64 / Python 3.10 和 Apple Silicon macOS arm64 / Python 3.12；包声明支持 Python 3.10–3.12。

服务使用单 worker 串行处理转录任务。JSON 文件保存任务状态，文件锁保证同一 workspace 只被一个实例占用。任务状态遵循以下规则：

- 排队任务在服务重启后继续。
- 运行中断的任务标记为 `failed/interrupted`，已落盘材料保留。
- 取消任务时，服务等待进程组清理结束后再处理下一项。
- 完成任务的转录和截图可在重启后读取。

读取接口兼容旧版转录材料，读取时不改写原文件。客户端通过任务身份保护笔记目录，重取规则见[使用指南](USAGE.md#阅读和保留材料)。

## 开发与运维入口

按工作内容选择对应入口：

- 配置服务和客户端：参见[配置参考](CONFIGURATION.md)。
- 安装、升级和备份：参见[部署指南](DEPLOYMENT.md)及 `deploy/` 模板。
- 修改代码和检查行为：参见[贡献与 Agent 指南](../AGENTS.md)及 `tests/`。
- 核对真实机器验收：参见[材料验收记录](VALIDATION.md)和[原生部署验收记录](VALIDATION-NATIVE.md)。

项目保留 [LICENSE](../LICENSE) 和 [NOTICE](../NOTICE) 中的许可与上游归属。模型和运行依赖的来源见[模型指南](MODELS.md#来源与许可)。
