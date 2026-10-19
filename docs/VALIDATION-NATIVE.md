# 原生部署与阅读验收（2026-10-20）

本轮使用同一 wheel，在两个新建目录中各创建单一 venv、安装 `[asr]`、独立下载默认模型，并在源码目录外运行服务。候选端口为 18122，数据与模型目录独立。原有 18121 生产服务持续运行，验收结束时健康检查为 200，MainPID 未变化。

## 安装与模型

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

实际安装的 wheel SHA-256：`9e0aa84a4e999849cc81c75f843048e40eeddb856cbf8f28b522c6968948b0e8`，对应运行代码提交 `6e4b193`。之后的部署模板修正只改变 systemd 关停配置，候选 unit 同步修正；未更改该 wheel 中的 Python 运行代码。

两机均为 funasr-onnx 0.4.3、onnxruntime 1.23.2、numpy 1.26.4、soundfile 0.14.0。模型来源与固定修订见 [MODELS.md](MODELS.md)；SenseVoice、BPE、VAD 主文件的 SHA-256 分别为：

- `21dc965f689a78d1604717bf561e40d5a236087c85a95584567835750549e822`
- `aa87f86064c3730d799ddf7af3c04659151102cba548bce325cf06ba4da4e6a8`
- `5289eb2aa3c9af2d7a4284bcfa7c3ceb81d360814ed4203239b6c5d0569da8a1`

任务 provenance 保存全部 10 个资产哈希、模型清单指纹、参数及实际软件版本。两机均未复制旧实验环境的模型或 venv。

## 真实输入与推理

以下均为本轮新建任务，成功完成、识别段非空，带合法 start/end/text；时间来源为 `VAD_interval`。耗时单位为秒，包含本次真实运行的模型加载，不作为跨硬件性能基准。

| 平台 / 输入 | task_id | 时长 | 段数 | 准备 | ASR | 总计 | 模型加载 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Mac，本地 20 秒视频上传 | `0b1d67e948164da19b8da69bb9b2d68c` | 20.000 | 1 | 0.149 | 1.983 | 2.263 | 1.147 |
| N100，B站第 3 P | `9710157ba6374b4c8ba526556c1ac237` | 1007.672 | 173 | 13.551 | 48.902 | 62.618 | 2.459 |
| N100，抖音 | `9107cbc732b4432db8e320460dfac859` | 679.134 | 49 | 53.956 | 38.682 | 92.809 | 2.306 |
| N100，本地上传及取消后续跑 | `94a4b936f8a04694aceb871cbfae857b` | 20.000 | 1 | 0.208 | 5.476 | 5.849 | 2.363 |

B站来源：[BV1dK411K7Fe，第 3 P](https://www.bilibili.com/video/BV1dK411K7Fe/?p=3)，题名为“线性代数的几何”。来源链接保留 `p=3`；来源 API 的 cid 为 232639680，时长 1008 秒，与实际媒体 1007.672 秒相符。该视频未在此前生产任务和本机笔记中处理过，同时满足非首 P 和全新至少 10 分钟视频要求。

抖音来源：[本轮教程输入](https://v.douyin.com/NCPT3XKVZMs/)。任务的 `acquisition_provider=galaxy_api`，实际触发配置允许的 Galaxy 回退；B站为 `upstream`，本地为 `upload`。本地上传的源视频保留在任务目录。

## 生命周期与兼容

只操作独立候选 unit。真实长音视频上传作为运行中任务，本地短视频作为排队任务；没有用假后端替代以下检查。

| 检查 | 实际结果 |
| --- | --- |
| 取消运行中的 ASR | `bea32ed0d1d04ea08bf073667b7d92e6` 变为 cancelled，所属子进程被回收；`94a4b936f8a04694aceb871cbfae857b` 随后 completed |
| systemctl stop / start | `cbb3204e1eb3420db78bb7a825370db0` 为 failed/interrupted；排队的 `1489b01d360145d7a4ece218d998f67e` 重启后 completed |
| SIGKILL 候选控制组、自动重启 | `46400e475eb1466a8f3615e2862a3949` 恢复为 failed/interrupted；排队的 `27786af89ca149fa8826bbf2944a2392` completed；旧 ASR 子进程不存在 |
| 完成材料重启可读 | 长视频 transcript.json 和截图前后 SHA-256 一致 |
| 第二实例，同 workspace、独立端口 18123 | 启动失败，明确报 Workspace already in use，未发生端口冲突 |
| 旧任务副本回读 | `a24104167838469d96de246481d08cd6` 的无版本材料按 v1 读取，127 段可经 MCP 获取，3 张旧截图可读；源目录、副本全部文件读取前后 SHA-256 一致 |
| MCP 与客户端 | 两个候选 /mcp 均暴露 9 个工具；三个输入任务独立抽帧，get_frame 原生 image/jpeg 与客户端下载文件 SHA-256 一致 |

真实停机验收发现并修正了一项部署问题：systemd 默认 `KillMode=control-group` 会先终止 ASR 子进程，使运行任务变成 asr_failed，队列可能在服务关停前启动下一项。模板改为 `KillMode=mixed` 后，同一真实检查通过：先由主进程停止队列并清理子进程，超时再强制清理控制组。此修正已同步到候选 unit。

验收脚本还修正了两个夹具问题：B站 preview.mp4 只有视频流，生命周期输入改为原有音视频无重编码合并后的上传文件；Ubuntu systemd 249 使用 `--kill-who=all`。旧材料缺少 schema_version 时依据兼容契约按 v1 处理。以上脚本问题没有引起生产改动。

## 新视频笔记与阅读

从 sdist 提取 Skill 和标准库客户端文件；用客户端获取本轮材料、用 MCP 分页读完 173 段。输出位于本机忽略目录：

- `workspace/notes/2026-10-20-mit-geometry-native-4f5529/notes.md`
- `workspace/notes/2026-10-20-mit-geometry-native-4f5529/chapters-transcript.md`
- 同目录 `materials/` 中保存原始转录、ASR 输出、元信息、截图及纠错依据。

笔记共 8 章，逐张查看 8 张章节截图后编写图注。所有原始识别文本按顺序完整覆盖，保留短片段及误识别；规范数学表达与不确定听辨记录在 corrections.md。素材署名 MIT OpenCourseWare，附 [原课程](https://ocw.mit.edu/courses/18-06sc-linear-algebra-fall-2011/)、[使用条款](https://ocw.mit.edu/pages/privacy-and-terms-of-use/)和 CC BY-NC-SA 4.0 链接。

自动核对结果：173 段分页拼接等于完整 segments；分章原稿按序覆盖 173 段；53 处本地文件、图片、章节锚点链接可解析；截图 relative_path 均相对笔记根目录，文件存在且哈希一致。追加截图并再次 fetch 后，notes.md、chapters-transcript.md、corrections.md、transcript.json 与 asr.raw.json 的 SHA-256 均未变化。最终索引有 10 张截图，其中 8 张用于章节，2 张为追加验证。

用户于 2026-10-20 回复：**“通过，阅读体验符合预期”**。该结论单独作为人工阅读验收，不由自动文件检查替代。

## 回归与证据

- 完整 pytest：133 passed，2 个可选发行包测试默认 skipped；保留一个既有 Starlette TestClient 弃用警告。
- mypy：18 个运行模块通过。
- 可选发行包检查：独立运行 2 passed；检查 wheel/sdist 内容与源码目录外安装。wheel/sdist 构建成功。
- Ubuntu systemd-analyze verify 退出 0；系统中已有的其他 unit 警告单独记录。
- GitHub Actions 已配置 Ubuntu Python 3.10/3.12 回归与发行物检查；本轮没有推送，尚未获得云端 CI 运行结果。

完整私有证据保存在本机 `workspace/validation-native/2026-10-20/`，包括 native-validation.json、两机 environment.txt、prepare/install 日志、native-lifecycle.json、native-frames.json、long-pagination.json、native-reading-check.json、native-legacy-check.json 和回归输出。任务原始文件分别保存在各自候选 workspace；阅读材料保存在上面的笔记目录。上述目录被 Git 忽略，公开记录不包含环境凭据或用户媒体文件。
