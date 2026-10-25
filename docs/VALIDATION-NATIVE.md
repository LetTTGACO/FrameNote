# 原生部署与阅读验收记录

## 2026-10-25：字幕优先与按需媒体

本次针对字幕优先分支的新代码，使用本地隔离候选服务（loopback `18122`、单 worker、独立 workspace）验证。未部署、未替换正式服务、未推送；下方 2026-10-20 的两机历史证据不计为本功能的新代码验收。运行代码基于 `ff5b5ac`；本次只修改发行检查与公开文档。

### 环境与离线验证

| 项目 | 本次实际结果 |
| --- | --- |
| 本机 | macOS 26.7.1（25G241），Apple Silicon arm64 |
| 原生运行环境 | Python 3.12.13，隔离 editable 环境，真实 SenseVoice ONNX；模型按固定清单 10/10 核验 |
| 最低版本兼容 | 同机独立 Python 3.10.20；属于离线行为回归，不代表 N100 原生推理 |
| Python 3.12 完整 pytest | `541 passed, 2 skipped, 1 warning in 44.78s` |
| Python 3.10 完整 pytest | `541 passed, 2 skipped, 1 warning in 61.59s` |
| mypy | `Success: no issues found in 25 source files` |
| 启用发行检查 | `2 passed in 29.77s`；独立构建 wheel/sdist、离线安装至新虚拟环境，在源码目录外执行 |
| 发行内容 | 六个新增运行模块、模型锁、LICENSE/NOTICE 齐全；私有目录、配置、凭据和任务/模型数据排除；MCP 12 工具，`credentials --help` 不加载推理库 |
| 构建 | wheel/sdist 构建成功；最终归档哈希记录在本次本地交付报告中 |

完整测试的 2 项 SKIP 是默认关闭的发行检查，不能计为通过；上表单独启用的 `2 passed` 才是发行证据。两套完整测试都保留一条既有 Starlette TestClient 弃用警告。首次启用旧发行测试实际失败于 `12 != 9`，更新后的断言再通过；归档缺模块与混入私有文件的负向探针另行执行。未执行云端 CI；现有 CI 保持 Ubuntu Python 3.10/3.12，3.12 单独启用发行测试。

### 真实本地推理与重启复读

先前同分支 `480e697` 的本地 20.000 秒 h264+aac 上传任务 `b60b0426a0e14b48a41742b8c90e11de` 已实际完成：SenseVoice ASR 1.621 秒、模型加载 0.881 秒、1 段 `VAD_interval`。本次比对 service、pipeline、acquire、media_jobs、frame_extractor 五个核心文件，SHA-256 全部与该次推理证据相同，复用该推理证据。

本次启动最新代码后 `/api/ready=200`，`configured/available=true`；通过真实 `/mcp` 读取到 12 个工具。回读旧任务，并重复请求相同帧点与视频，仍返回已完成的原操作：

- 截图 operation `b8f96296926844fbac14e095b4f39d14`，请求 1.0 / 5.123 秒，实际 PTS 为 1.0 / 5.133333 秒。
- 有声视频 operation `8513d5c7b4fe405788efda9266d4b1af`，原下载 8,476,849 字节，ffprobe 为 20.000 秒且含 h264 视频和 aac 音频。
- 重启及读取后 `transcript.json` SHA-256 仍为 `72a0d898bfd6d9db3c6112265a46ad60763b14032b6ef6b0629ae675c3e34432`，`asr.raw.json` 仍为 `9e503cea656e0307f0bbb0c8125931a656d49f8ac91d43c7f4efe198699a2f17`。

### 真实匿名 B站回退

关闭 Galaxy、清除凭据环境变量后提交 [BV1Qwby6DEu1，第 1 P](https://www.bilibili.com/video/BV1Qwby6DEu1?p=1)。本次解析身份为 `aid=117100001691581 / cid=40943289185 / page=1`，平台时长 204 秒。字幕接口实际返回需要登录，任务记录 `fallback_reason=auth_required`，随后从平台获取媒体并实际运行 ASR；这不构成 `no_tracks` 证据。

| 项目 | 本次实际结果 |
| --- | --- |
| task_id | `84ad81f234e04d1db2966b80930bf813`，completed |
| 转录 | 26 段，`VAD_interval`，ASR schema v2；四页（每页 7 段）拼接等于完整 segments |
| 准备 / ASR / 总计 | 2.202 / 4.404 / 6.734 秒；提交至轮询终态墙钟 8.055 秒 |
| 获取路径 | upstream；yt-dlp 受平台风控后改用 playurl 视频流与音频流；Galaxy 关闭 |
| 截图 operation | `308bb83c23dd4b8798f1f68fb7e6fa61`，请求 1.0 / 5.123 秒，实际 PTS 1.005250 / 5.126875 秒 |
| 视频 operation | `b08012e8472242bfb28a8cfe623b1bdf`，completed |
| 有声视频 | 6,727,179 字节；203.265813 秒，h264 视频 + aac 音频 |
| 下载 SHA-256 | `676886d8cc8cd15082f32ae81a2eb76a38748df92ecef3dbb6f70a384ae262c6` |
| transcript.json SHA-256 | `8163e0a64ef76264a871ccd192beb5dad2c9af991d55a7e131ccf6e9653704c8` |
| asr.raw.json SHA-256 | `d74a96d85ec8667eb50b9e648788754313db922a2d6f0a46cee4720d87c3d7e0` |

截图和视频准备后两份原件哈希不变。该任务经过 ASR 获取路径，已有媒体可复用；不能据此声称已验证“字幕任务首次按需下载且视频只下载一次”。

### 合成无效 Cookie

在专用临时私有目录创建固定测试值，通过 `framenote.credentials import` 导入：父目录 0700、文件 0600，`status` 返回 `configured=true / usable=true`，仅说明本地格式合法。新隔离实例使用该路径后，真实字幕接口返回 `auth_required`，任务 `686d898a42b746cc96af0b2d7aeabdd4` 自动回退并完成 26 段 ASR，准备 / ASR / 总计为 2.087 / 4.105 / 6.303 秒。逐文件检查任务材料和日志未出现合成值。

这是合成无效 Cookie 的路径与回退验证，没有使用真实账号，也不代表真实登录过期验收。两个本次拥有的候选进程均正常 SIGTERM 停止，测试凭据和输入已用本地 clear 及文件清理回收；未接触其他凭据。

### 待完成的真实验收

本次没有取得新授权的有效登录文件，以下检查仍待完成：有效字幕阳性任务（无提前下载媒体和 ASR 原件、分页与原始字幕哈希）、本次认证响应确认的 `no_tracks`、真实登录失效副本、字幕任务后续截图与有声视频及仅下载一次、force-ASR 新任务保留旧字幕目录。历史字幕段数和候选视频只作线索，不作为通过证据。

N100 Ubuntu x86_64 / Python 3.10 的新代码原生验证也未执行：当前授权排除部署和远端启动/替换。2026-10-20 的旧代码 N100 验收与本机 Python 3.10 行为测试分别记录，不能替代该项。

完整本地证据保存在隔离验收工作区的 `native-probes/`（`local-native-smoke.json`、`final-restart-cache.json`、`anonymous-asr-smoke.json`、`anonymous-media-smoke.json`、`synthetic-credential-management.json`、`synthetic-invalid-smoke.json`）及交付报告。公开记录只保留脱敏事实，不提交媒体、凭据或本地验收目录。

## 2026-10-25：最终评审修复复验

本轮基于 `faa4f539d34b30b50bedfc654f7132ff8e7705aa`，只修复元数据暂时不可达后旧获取路径的无效分 P 处理、重复原截图请求的部分结果继承，以及单次请求超时的有限重试分类。上方 `ff5b5ac` 的原生推理和五文件一致性比对属于当时的历史证据；本轮修改了 `service_acquire.py` 与 `media_jobs.py`，不能把该一致性声明延伸到当前源码。

最终源码在本地 Python 3.12.13 与 3.10.20 完整 pytest 均为 `566 passed, 2 skipped, 1 warning`，退出码均为 0；mypy 为 `Success: no issues found in 25 source files`。两项默认跳过仍是可选发行检查，一条警告仍是原有 Starlette TestClient 依赖弃用提示。完整日志、最终归档与单独启用发行检查的结果保存在本轮本地交付报告中。

复用上述 20 秒本地任务 `b60b0426a0e14b48a41742b8c90e11de`，在 loopback `18122` 启动最终源码的隔离 editable 服务，`/api/ready` 实际返回 200、SenseVoice configured/available 均为 true。本次未重复 ASR，也未访问外部平台。使用新的 `[3.345, 13.345]` 帧点、`background=true`，通过真实 HTTP POST 验证部分失败后的原命令重试：

- 隔离启动脚本在第二个帧点首次调用时，把解码器输入替换为一个不存在的本地文件，真实解码子进程因此失败。运行源码和原媒体未改写；这是一项明确注入失败的本地恢复验证。
- 原操作 `874e46f03a4349d3bc72f9f49fdefbf0` 失败并保留第一帧；重复完全相同的 POST 创建 `0fba87f5d63f4f0990f82e763736a645`，其 `retry_of` 指回原操作，随后完成。
- 第一帧 ID `821fc27dc4d046f2b9fb319cca1a7952`、实际 PTS `3.366667`、121,667 字节及 SHA-256 `bb17c46d48afb2c0be93dfb1772b35b05373bb7ac37e22fb15d1fe752477a861` 均保留。第二帧实际 PTS 为 `13.366667`；解码器调用序列恰为 `[3.345, 13.345, 13.345]`。
- 原失败操作回读不变；第三次相同 POST 返回 HTTP 200 和已完成重试缓存。`transcript.json`、`asr.raw.json`、`source.mp4` 的前后 SHA-256 均相同，分别为 `72a0d898bfd6d9db3c6112265a46ad60763b14032b6ef6b0629ae675c3e34432`、`9e503cea656e0307f0bbb0c8125931a656d49f8ac91d43c7f4efe198699a2f17`、`6110e4ae401eaea855db6586acfe21d0107dde7dad2d8ec529bac7260e331425`。
- 拥有的服务收到 SIGTERM 后完成 application shutdown，进程返回 `-15`，端口确认关闭。

本次实际测试的 `media_jobs.py` SHA-256 为 `5543f303015528b84bfa03a48f1c4cc210b13e268bd64a05d5d63cdcec3283d2`；四个修改后运行模块的完整哈希、操作对象、调用序列和关闭记录见本地 `native-probes/final-fix-native.json`。初次启动脚本误将正常 SIGTERM 的 `-15` 当作断言失败，随后修正该验证假设、保留初次日志，并以新帧点重新完成全部检查。

上述有效登录字幕阳性、认证 `no_tracks`、真实账号过期、真实字幕任务的按需媒体与 force-ASR 保留，以及 N100 新代码原生验收仍待完成。本轮本地恢复测试不能替代这些平台和部署组合的验收。

## 2026-10-20：历史版本验收

本记录供维护者复核 FrameNote 0.1.0 在 Ubuntu N100 和 Apple Silicon Mac 上的安装、推理、任务生命周期及笔记阅读结果。初验和最终复验分别绑定 wheel 哈希与代码提交；其中的 MCP 调用和测试数量描述对应版本的历史行为。部署操作见[部署指南](DEPLOYMENT.md)。

### 验收范围

两台机器使用同一个 wheel，分别在新目录创建单一虚拟环境、安装 `[asr]`、独立下载模型，并在源码目录外运行服务。候选服务使用端口 `18122`，数据与模型目录独立。

原有 `18121` 生产服务在验收期间持续运行。验收结束时，其健康检查返回 `200`，`MainPID` 未变化。

### 安装与模型

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

### 真实输入与推理

初验的四个任务均成功完成，识别段非空，包含合法的 `start`、`end` 和 `text`，时间来源为 `VAD_interval`。各项耗时以秒计，包含模型加载，仅用于记录这次运行：

| 平台 / 输入 | task_id | 时长 | 段数 | 准备 | ASR | 总计 | 模型加载 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Mac，本地 20 秒视频上传 | `0b1d67e948164da19b8da69bb9b2d68c` | 20.000 | 1 | 0.149 | 1.983 | 2.263 | 1.147 |
| N100，B站第 3 P | `9710157ba6374b4c8ba526556c1ac237` | 1007.672 | 173 | 13.551 | 48.902 | 62.618 | 2.459 |
| N100，抖音 | `9107cbc732b4432db8e320460dfac859` | 679.134 | 49 | 53.956 | 38.682 | 92.809 | 2.306 |
| N100，本地上传及取消后续跑 | `94a4b936f8a04694aceb871cbfae857b` | 20.000 | 1 | 0.208 | 5.476 | 5.849 | 2.363 |

B站输入为[“线性代数的几何”，第 3 P](https://www.bilibili.com/video/BV1dK411K7Fe/?p=3)。来源链接保留 `p=3`，来源 API 的 `cid=232639680`，标示时长 1008 秒，与媒体时长 1007.672 秒相符。该输入满足这次验收的非首 P、至少 10 分钟、此前未在生产任务和本机笔记中处理过的条件。

抖音输入为[AI 视频教程](https://v.douyin.com/NCPT3XKVZMs/)，任务的 `acquisition_provider=galaxy_api`，在该版本中触发配置允许的 Galaxy 回退。B站使用 `upstream`，本地输入使用 `upload`；上传的视频保留在任务目录。

### 生命周期与兼容

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

### 新视频笔记与阅读

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

### 回归与证据

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

### 独立审查后的最终复验

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
