# 原生部署与阅读验收记录：2026-10-20

本记录供维护者复核 FrameNote 0.1.0 在 Ubuntu N100 和 Apple Silicon Mac 上的安装、推理、任务生命周期及笔记阅读结果。初验和最终复验分别绑定 wheel 哈希与代码提交；其中的 MCP 调用和测试数量描述对应版本的历史行为。部署操作见[部署指南](DEPLOYMENT.md)。

## 验收范围

两台机器使用同一个 wheel，分别在新目录创建单一虚拟环境、安装 `[asr]`、独立下载模型，并在源码目录外运行服务。候选服务使用端口 `18122`，数据与模型目录独立。

原有 `18121` 生产服务在验收期间持续运行。验收结束时，其健康检查返回 `200`，`MainPID` 未变化。

## 安装与模型

初验环境及检查结果如下：

| 项目 | N100 | Apple Silicon Mac |
| --- | --- | --- |
| OS / 架构 | Ubuntu 22.04.5 LTS，Linux 6.8.0-124，x86_64 | macOS 26.7.1（25G241），arm64 |
| Python | 3.10.12 | 3.12.13 |
| 安装包 | framenote 0.1.0 wheel，`[asr]` | 相同 wheel，`[asr]` |
| 运行方式 | 独立候选 systemd unit，局域网绑定，单 worker | uvicorn，loopback 绑定，单 worker |
| 安装位置 | 候选 venv 的 site-packages | 候选 venv 的 site-packages |
| pip check | No broken requirements found | No broken requirements found |
| models prepare / verify | 公共固定来源独立下载，10/10 通过 | 公共固定来源独立下载，10/10 通过 |
| ready | 200，configured/available=true | 200，configured/available=true |

初验使用的 wheel SHA-256 为
`9e0aa84a4e999849cc81c75f843048e40eeddb856cbf8f28b522c6968948b0e8`，运行代码提交为 `6e4b193`。初验后的部署模板修正仅调整 systemd 关停配置，候选 unit 同步更新；该 wheel 的 Python 代码未改变。

两台机器安装的推理依赖均为 `funasr-onnx 0.4.3`、`onnxruntime 1.23.2`、`numpy 1.26.4` 和 `soundfile 0.14.0`。模型来源和固定提交见[模型指南](MODELS.md)。三个主文件的 SHA-256 为：

- SenseVoice：`21dc965f689a78d1604717bf561e40d5a236087c85a95584567835750549e822`。
- BPE：`aa87f86064c3730d799ddf7af3c04659151102cba548bce325cf06ba4da4e6a8`。
- VAD：`5289eb2aa3c9af2d7a4284bcfa7c3ceb81d360814ed4203239b6c5d0569da8a1`。

任务 `provenance` 保存全部 10 个资产哈希、模型清单指纹、参数和实际软件版本。两台机器均独立安装环境和模型，未复制旧实验的虚拟环境或权重。

## 真实输入与推理

初验的四个任务均成功完成，识别段非空，包含合法的 `start`、`end` 和 `text`，时间来源为 `VAD_interval`。各项耗时以秒计，包含模型加载，仅用于记录这次运行：

| 平台 / 输入 | task_id | 时长 | 段数 | 准备 | ASR | 总计 | 模型加载 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Mac，本地 20 秒视频上传 | `0b1d67e948164da19b8da69bb9b2d68c` | 20.000 | 1 | 0.149 | 1.983 | 2.263 | 1.147 |
| N100，B站第 3 P | `9710157ba6374b4c8ba526556c1ac237` | 1007.672 | 173 | 13.551 | 48.902 | 62.618 | 2.459 |
| N100，抖音 | `9107cbc732b4432db8e320460dfac859` | 679.134 | 49 | 53.956 | 38.682 | 92.809 | 2.306 |
| N100，本地上传及取消后续跑 | `94a4b936f8a04694aceb871cbfae857b` | 20.000 | 1 | 0.208 | 5.476 | 5.849 | 2.363 |

B站输入为[“线性代数的几何”，第 3 P](https://www.bilibili.com/video/BV1dK411K7Fe/?p=3)。来源链接保留 `p=3`，来源 API 的 `cid=232639680`，标示时长 1008 秒，与媒体时长 1007.672 秒相符。该输入满足这次验收的非首 P、至少 10 分钟、此前未在生产任务和本机笔记中处理过的条件。

抖音输入为[AI 视频教程](https://v.douyin.com/NCPT3XKVZMs/)，任务的 `acquisition_provider=galaxy_api`，在该版本中触发配置允许的 Galaxy 回退。B站使用 `upstream`，本地输入使用 `upload`；上传的视频保留在任务目录。

## 生命周期与兼容

生命周期检查只操作候选 unit，使用真实长音视频作为运行任务、本地短视频作为排队任务。检查结果如下：

| 检查 | 实际结果 |
| --- | --- |
| 取消运行中的 ASR | `bea32ed0d1d04ea08bf073667b7d92e6` 变为 cancelled，所属子进程被回收；`94a4b936f8a04694aceb871cbfae857b` 随后 completed |
| systemctl stop / start | `cbb3204e1eb3420db78bb7a825370db0` 为 failed/interrupted；排队的 `1489b01d360145d7a4ece218d998f67e` 重启后 completed |
| SIGKILL 候选控制组、自动重启 | `46400e475eb1466a8f3615e2862a3949` 恢复为 failed/interrupted；排队的 `27786af89ca149fa8826bbf2944a2392` completed；旧 ASR 子进程不存在 |
| 完成材料重启可读 | 长视频 transcript.json 和截图前后 SHA-256 一致 |
| 第二实例，同 workspace、独立端口 18123 | 启动失败，明确报 Workspace already in use，未发生端口冲突 |
| 旧任务副本回读 | `a24104167838469d96de246481d08cd6` 的无版本材料按 v1 读取，127 段可经 MCP 获取，3 张旧截图可读；源目录、副本全部文件读取前后 SHA-256 一致 |
| MCP 与客户端 | 两个候选 /mcp 均暴露 9 个工具；三个输入任务独立抽帧，get_frame 原生 image/jpeg 与客户端下载文件 SHA-256 一致 |

真实停机检查发现：systemd 默认 `KillMode=control-group` 会先终止 ASR 子进程，使运行任务记为 `asr_failed`，队列可能在服务关停前启动下一项。模板改为 `KillMode=mixed` 后，同一检查通过：主进程先停止队列并清理子进程，超时后由 systemd 强制清理控制组。候选 unit 已应用该修正。

验收脚本另有两项修正：B站 `preview.mp4` 只有视频流，因此生命周期输入改为将已有音视频无重编码合并后的文件；Ubuntu systemd 249 使用 `--kill-who=all`。读取旧材料时，缺少 `schema_version` 的材料按 v1 契约处理。这些脚本修正未改变生产环境。

## 新视频笔记与阅读

验收从源码归档提取 Skill 和标准库客户端，用客户端获取材料，并通过 MCP 分页读取全部 173 段。阅读输出保存在以下本地目录：

```text
workspace/notes/2026-10-20-mit-geometry-native-4f5529/
├── notes.md
├── chapters-transcript.md
└── materials/
```

`materials/` 保存原始转录、ASR 输出、元信息、截图和纠错依据。笔记分为 8 章，逐张查看 8 张章节截图后编写图注。分章原稿按顺序覆盖全部识别文本，保留短片段与误识别；规范数学表达和不确定听辨记录在 `corrections.md`。

素材署名为 MIT OpenCourseWare，笔记附有[原课程](https://ocw.mit.edu/courses/18-06sc-linear-algebra-fall-2011/)、[使用条款](https://ocw.mit.edu/pages/privacy-and-terms-of-use/)及 CC BY-NC-SA 4.0 链接。

自动检查确认以下结果：

- 173 段分页拼接结果等于完整 `segments`，分章原稿按序覆盖全部 173 段。
- 53 处本地文件、图片和章节锚点链接可解析。
- 截图 `relative_path` 相对笔记根目录，目标文件存在且哈希一致。
- 追加截图并再次执行 `fetch` 后，`notes.md`、`chapters-transcript.md`、`corrections.md`、`transcript.json` 和 `asr.raw.json` 的 SHA-256 均未变化。
- 最终截图索引包含 10 张图片，其中 8 张用于章节，2 张用于追加验证。

用户于 2026-10-20 确认：**“通过，阅读体验符合预期”**。这项人工阅读结果独立于自动文件检查记录。

## 回归与证据

验收结束时记录的检查结果如下：

| 检查 | 结果 |
| --- | --- |
| 完整 pytest | `144 passed`；2 项可选发行包测试默认跳过；有一条既有 Starlette TestClient 弃用警告 |
| mypy | 18 个运行模块通过 |
| 可选发行包检查 | 单独运行 `2 passed`，覆盖 wheel/sdist 内容与源码目录外安装 |
| 构建 | wheel 和源码归档构建成功 |
| systemd unit | Ubuntu 上 `systemd-analyze verify` 退出 `0`；其他已有 unit 的警告单独记录 |
| 云端 CI | 已配置 Ubuntu Python 3.10/3.12 回归与发行物检查；这次验收未推送，未执行云端 CI |

完整私有证据位于 `workspace/validation-native/2026-10-20/`，包括：

- `native-validation.json`、两机 `environment.txt` 和模型准备及安装日志。
- `native-lifecycle.json`、`native-frames.json`、`long-pagination.json`。
- `native-reading-check.json`、`native-legacy-check.json` 和回归输出。

任务原始文件保存在各自候选 workspace，阅读稿保存在[新视频笔记与阅读](#新视频笔记与阅读)所列目录。这些目录由 Git 忽略，公开记录仅保留验收事实，不包含环境凭据或用户媒体文件。

## 独立审查后的最终复验

一次全新上下文的整分支审查提出 3 项 Important 问题，Critical 和 Minor 均为 0。复现后完成以下修复：

- 分页响应的 `text` 按当前页生成。
- 兼容 Python 3.10 的 asyncio 超时类型，保留 `asr_timeout` 错误码。
- 用独立且受保护的任务回收子进程；重复取消时，继续等待 TERM/KILL 和进程退出。

真实抵抗 TERM 的子进程覆盖“先取消、清理期间再停服”的重叠时序。修复新增 11 个回归用例，最终完整套件、可选发行检查和类型检查结果见[回归与证据](#回归与证据)。

Python 3.10 的真实 `asyncio.wait_for` 回归在修复前失败。安装最终 wheel 后，N100 在源码目录外运行 service/adapter 针对性测试，结果为 `41 passed`。额外测试依赖在干净安装初验后加入候选虚拟环境，独立于服务运行依赖。

最终运行代码提交为 `2c14ea6`，wheel SHA-256 为
`40f92fae760583939ad40bdca6852c809b2cbc6c48ba9a80bff44566d7e99ae8`。
两机强制重装同一最终 wheel，`pip check` 均通过，仅重启各自 `18122` 候选服务。最终本地视频转录结果如下：

| 平台 | task_id | 段数 | 准备 / ASR / 总计（秒） |
| --- | --- | ---: | --- |
| Mac | `e4f9d119e0b947989e41f51aca85ffb1` | 1 | 0.118 / 1.744 / 1.990 |
| N100 | `70867954728c48c2ad8aa928d3e18a62` | 1 | 0.212 / 5.339 / 5.712 |

通过 N100 的 `/mcp` 回读初验长视频时，`offset/limit`、时间筛选和空页响应中的 `text` 均等于所选 `segments` 的拼接；原始下载文件保留全文。阅读稿未修改，人工验收结论保持有效。

最终复验再次覆盖取消、正常停机与重启、强杀恢复、工作目录锁和生产健康检查。结果保存在 `final-lifecycle.log` 与 `native-lifecycle.json`；`final-native-retest.json`、`final-py310-green.log` 保存最终 wheel 和机器证据。初验与最终复验记录分别保留，验收结束时云端 CI 尚未执行。
