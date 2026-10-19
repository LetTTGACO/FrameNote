# FrameNote 0.1.0 验收记录

日期：2026-10-20。服务实际运行于 Homelab Ubuntu / N100，Mac 经局域网调用 `http://192.168.31.56:18121`。本次独立运行 FrameNote 代码与服务环境，ASR 复用现有 SenseVoiceSmall ONNX INT8、FSMN VAD 权重及 ASR Python 环境。

## 三种真实输入

| 输入 | 获取方式 | 视频长度 | 总耗时 | ASR 耗时 | 转录段数 | 新任务 |
| --- | --- | --- | --- | --- | --- | --- |
| [B站：做减法之后的Obsidian插件有多纯粹](https://www.bilibili.com/video/BV1cfaf6SEA9/?p=1) | 平台下载器 | 1715.03 秒 | 102.14 秒 | 90.48 秒 | 127 | a24104167838469d96de246481d08cd6 |
| [抖音：0基础，带你做出你人生第一个AI视频](https://v.douyin.com/NCPT3XKVZMs/) | Galaxy API 回退 | 679.13 秒 | 75.76 秒 | 35.67 秒 | 49 | 3ff0dcf4877b4d9fbc989d4c401552c4 |
| Mac 本地 MP4 片段 | 标准库客户端流式上传 8,476,849 字节 | 20 秒 | 5.45 秒 | 2.57 秒 | 1 | f898170f4255432d96c1a3363034cf43 |

三项均重新获取媒体、执行转录后完成；三份 transcript.json 与旧实验同视频结果逐字段一致。耗时为本次实际观测，包含获取、提音、模型加载与转录，不是准确率评测或性能保证。

## MCP、Skill 与笔记

- 新服务通过 Streamable HTTP 握手，server name 为 FrameNote，发现 9 个 MCP 工具。
- 通过 MCP 独立抽取 B站 3 张、抖音 20 张、本地片段 3 张；每种输入均验证 get_frame 返回原生 image/jpeg，与下载文件的 SHA-256 相同。
- Skill 的标准库客户端将三份材料取回 Mac，frames.json 的 relative_path 相对笔记根目录。
- 使用新任务材料生成本地片段阅读稿；同视频抖音对照稿沿用已校对章节组织，替换为新任务及重新抽取的图片。20 张图与原实验图像哈希一致。
- 两份输出共检查 67 个本地文件及章节锚点链接；抖音分章原稿完整保留 49 段识别文本及原顺序。再次 fetch 保留两份阅读文档和 corrections.md。
- 本次材料位于本项目 workspace/notes/，完整机器记录位于 workspace/validation/results.json；这些个人材料由 Git 忽略。

## 回归与复核

Mac 独立环境：51 项测试通过；mypy 检查全部 13 个运行模块通过；Skill 官方校验器通过。规格与工程两项复核发现并修复 B站短链跳转分 P 丢失、父进程先退出时取消未回收整个进程组的问题，均已补行为测试并复核。

服务重启后复读持久任务与截图。旧实验的 7 个任务复制到新 workspace，原目录保留；通过旧 task_id 仍可在新服务读取原稿与图片。
