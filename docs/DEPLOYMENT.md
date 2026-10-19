# 当前部署

## 入口与目录

- Mac 源码：`/Users/1874w/workspace/studio/FrameNote`。
- Homelab 源码：`/root/labs/FrameNote`。
- systemd：`framenote.service`，监听 `0.0.0.0:18121`。
- REST：`http://192.168.31.56:18121`；健康检查 `/api/health`。
- MCP：`http://192.168.31.56:18121/mcp`，Streamable HTTP。
- 实际配置：Homelab 的 `/root/labs/FrameNote/service.env`。
- 任务：Homelab 的 `/root/labs/FrameNote/workspace/tasks/`。
- 笔记：Mac 的 `workspace/notes/`，每份包含两份阅读文档及 materials/。

当前 SenseVoice 运行环境和模型路径见 deploy/service.env.example。服务自身使用 FrameNote 的 .venv；推理子进程使用 FRAMENOTE_ASR_PYTHON 指定的独立 ASR 环境。更改服务地址时同步更新 service.env、项目 .codex/config.toml 和客户端服务地址。

## Codex 接入

FrameNote 的 .codex/config.toml 配置 MCP，.agents/skills/framenote 链接到项目内技能。当前 Labs 工作区的 MCP 与 Skill 入口也指向这份独立实现。全局配置只登记项目目录信任，MCP 与 Skill 保持项目范围。

配置变更后重启 Codex 以刷新当前聊天的 MCP 工具。刷新前可直接使用 skills/framenote/scripts/headless.py。用户发送链接后，Skill 在本项目 workspace/notes/ 保存结果；材料处理后由 Codex 完成章节笔记。

## 运维与历史任务

```bash
ssh homelab 'systemctl status framenote.service --no-pager'
ssh homelab 'journalctl -u framenote.service -n 60 --no-pager'
ssh homelab 'systemctl restart framenote.service'
```

旧实验位于 `/root/labs/video-to-note`，其 7 个任务已复制到新服务，原数据保留。旧 video-to-note.service 停止并取消开机启动；原服务 unit 和 18120 配置保留，恢复时先启动原服务，再把客户端切回 18120。Mac 旧实验源码及已有笔记保持原位置。

更新代码后在 Mac 运行 README 中的回归检查，部署同一提交到 Homelab，再重启 framenote.service。备份应覆盖 service.env 和 workspace/；代码与模型按各自来源管理。
