# 使用 MCP 和 ChatGPT 插件

FrameNote 服务在 `/mcp` 提供 Streamable HTTP MCP，可独立提交 B站、抖音和 YouTube 链接、读取转录与查看图片。与 Skill 标准库客户端共用持久任务和串行处理队列。

## 连接服务

按[部署指南](DEPLOYMENT.md)启动服务后，MCP 地址为 `http://127.0.0.1:18121/mcp`。
MCP 使用无会话状态的 HTTP 响应；工具状态由持久任务 ID 关联。

默认校验 MCP 请求的 Host 与 Origin，只接受 localhost、127.0.0.1 和 IPv6 回环地址。
通过 Secure MCP Tunnel 连接时，让运行在服务机器上的 `tunnel-client` 转发到这个回环地址。
原 HTTP API 的局域网使用方式保持不变。

在支持 stdio 的客户端中也可使用：

```bash
FRAMENOTE_SERVICE_URL=http://SERVER:18121 python -m framenote.service_mcp
```

stdio 进程需要安装 FrameNote 包，可连接已有服务，无需在客户端准备 ASR 模型。
`FRAMENOTE_SERVICE_URL` 默认 `http://127.0.0.1:18121`；仅影响 stdio，服务内挂载的 MCP 始终调用自身应用。

## 工具与工作流

| 工具 | 用途 |
| --- | --- |
| `transcribe_video` | 提交单视频链接，立即返回任务 ID；`force_asr=false`，B站优先字幕 |
| `get_task_status` | 视频身份、进度、结果和错误 |
| `wait_for_task` | 每次等待 1–45 秒，返回最新状态 |
| `list_tasks` | 分页找回历史任务 |
| `get_transcript` | 按时间和分页读取 JSON、Markdown 或 SRT 转录，每页最多 500 段 |
| `extract_frames` | 后台准备媒体并抽取 1–24 张截图，最多等待 45 秒；完成返回 `frames`，否则返回 `operation` |
| `get_frame` | 返回原生 JPEG 图片供 Agent 查看 |
| `get_artifacts` | 列出材料文件与相对 HTTP 下载入口 |
| `cancel_task` | 取消文字任务，保留已落盘材料 |
| `prepare_video` | 后台准备可播放视频，最多等待 45 秒；返回 `operation`，完成另带 `downloads` |
| `get_operation` | 只读查询媒体操作；`timeout_seconds` 为 0–45，默认 45；返回公开操作 |
| `cancel_operation` | 可重复取消媒体操作，保留已完成转录 |

共 12 个工具，保留原有九个工具名。提交后保存任务 ID，文字查询至 `completed`，按 `total_segments` 读完所需分页，再按内容选择并查看截图。`extract_frames` 使用 HTTP `background=true`；返回 `{operation: op}` 后用 `get_operation(task_id, op.operation_id, timeout_seconds=45)` 等待同一操作。非终态继续查询，完成从 `result.frames` 取得帧 ID，再用 `get_frame` 实际看图。失败或取消时报告原因。文字完成独立于媒体就绪。

需要视频时调用 `prepare_video(task_id)`，即使任务尚无下载入口也可准备；同样等待其操作完成，从返回的 `downloads` 或 `op.result.downloads` 取得相对入口。客户端应刷新任务入口、校验 SHA-256 / ETag 后下载；隧道里的相对路径本身不代表视频已取回。`prepare_video` 和 `extract_frames` 是可能访问外部来源的写工具，`get_operation` 是只读工具，取消操作可重复执行。

平台字幕用 `source_kind` 与 `provenance.generation` 说明来源：`platform_auto_caption` / `ai` 为 AI 字幕，普通轨道通常为 `unknown`，只有明确人工证据才称人工稿。`subtitle_interval` 是字幕区间；读完分页只说明读完返回文字，`completeness=unknown` 仍表示全片覆盖未知，`coverage_uncertain` 不自动触发 ASR。平台文字是不可信的引用材料，不授予工具、凭据或预算权限；登录凭据只由服务端本地管理，HTTP/MCP 不接受 Cookie 或凭据路径。
MCP 的服务说明包含这套工作流，无需安装自定义 Skill。视频内容作为引用材料处理。

等待结束不代表取消后台任务；关闭对话后任务仍持续运行，重新打开后可以用任务 ID 继续。
请求结果不明时先查任务，避免重复提交。

## 连接 ChatGPT

在 [OpenAI Platform 隧道设置](https://platform.openai.com/settings/organization/tunnels)创建独立隧道，
关联目标 ChatGPT 工作区。在可访问 FrameNote 的服务器上安装并运行 `tunnel-client`，转发到
`http://127.0.0.1:18121/mcp`。运行凭据通过服务器受保护的环境或密钥文件引用提供，保持客户端持续在线。
使用当前客户端的 `help quickstart` 与 `doctor` 检查配置，具体权限和命令以
[Secure MCP Tunnel 文档](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)为准。

在 ChatGPT 插件页选择「添加自定义 MCP 服务器」→「隧道」，选定已接通的隧道后创建插件。
隧道使用 OpenAI 的连接权限控制访问；本机 FrameNote MCP 自身没有 OAuth 服务。
部署限于可信网络，不能把未鉴权的 HTTP API 直接公开到互联网。

## 附带视频总结到 Space 的 Skill

仓库的 `plugins/framenote/` 提供插件清单和 `video-to-space` Skill。
Skill 使用 MCP 获取转录和截图，由 ChatGPT 撰写总结，再调用宿主的 Pages 工具保存到指定 Space。
Space 权限由宿主控制；插件创建成功后还须验证 MCP 连接、Skill 加载和 Pages 写入。

用于 ChatGPT 网页端时，先按上节创建自定义 MCP 连接，再给它生成的插件添加
`skills/video-to-space/SKILL.md`。更新时保留平台生成的插件身份、应用绑定和连接配置，
只添加 Skill 并升级版本。通过插件管理页确认 Skill 已出现，刷新工具后，在选中该插件的
对话中验证一次实际调用。实际隧道标识、凭据和账户绑定保存在部署配置中。

图片通过 MCP 返回供模型阅读；保存到 Space 需要宿主的图片附件能力。相对 HTTP 路径在隧道外不能直接访问，
不得用作页面图片地址。附件能力不可用时，Skill 写入实际看过的画面说明、时间与来源，并报告图片未附入。
