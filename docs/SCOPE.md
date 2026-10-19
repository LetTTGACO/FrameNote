# FrameNote 实施范围

来源：2026-10-20 用户确认的最小代码提取流程。

- 项目目录 `/Users/1874w/workspace/studio/FrameNote`，独立于实验仓库运行。
- 支持B站单视频/单分P、抖音链接、本地视频上传。
- Homelab常驻服务负责下载、SenseVoiceSmall转录、持久任务和独立抽帧。
- 抖音原版下载失败时，按配置回退Galaxy外部API。
- 同端口提供REST API和Streamable HTTP MCP，保留9个材料工具。
- Codex通过Skill生成首次整理的阅读底稿，保留关键内容、章节时间、截图和原稿引用。
- 笔记顶层为notes.md、chapters-transcript.md，其余材料归入materials/。
- 验证三种输入、MCP原生图像、材料下载、输出链接和任务回查。
- 保留旧实验目录与部署作对照；独立版本通过后创建GitHub仓库并切换部署。

代码组织：framenote/为服务与下载实现，skills/framenote/为Agent工作流，deploy/为systemd和配置示例，tests/为行为回归。
