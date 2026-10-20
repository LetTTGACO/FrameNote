# FrameNote 当前范围

FrameNote 提供 B站单视频/指定分 P、抖音、YouTube 单视频与本地上传的材料服务，包含可配置的 Galaxy 获取（抖音直接获取，B站和 YouTube 下载失败回退）、SenseVoiceSmall ONNX INT8 + FSMN VAD、持久任务、独立截图、HTTP API 与标准库 Skill 客户端。

服务原生安装，默认一个 venv 并通过独立子进程推理，单 worker 串行任务与 JSON 存储。目标环境为 N100 Ubuntu x86_64/Python3.10 与 Apple Silicon macOS arm64/Python3.12。固定10文件模型锁与 prepare/verify 随包交付，文档覆盖安装、运行、升级与备份。

ASRBackend 把具体模型检查/推理/raw映射封装在适配器中。开发者实现协议、配置解析与工厂分支后，可通过配置选择其后端；所有非空段落提供 start/end/text。

Skill 客户端支持用户级与项目级配置，以及默认关闭的按需视频取回。Agent 根据完整转录和实际截图生成两份阅读文档；笔记根目录为 notes.md、chapters-transcript.md、materials/。旧材料只读兼容，重取保留用户阅读文档和纠错记录。

代码在 framenote/，项目工作流在 skills/framenote/，部署模板在 deploy/，行为与发行回归在 tests/。LICENSE/NOTICE 保留上游来源。
