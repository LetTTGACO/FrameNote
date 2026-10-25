# 接入 ASR 后端

本指南供熟悉 Python 异步编程的开发者使用，说明如何为 FrameNote 接入自动语音识别（ASR）后端，并验证时间、原始材料和取消行为。内置实现为 SenseVoiceSmall ONNX INT8 + FSMN 语音活动检测（VAD）；部署该实现见[部署指南](DEPLOYMENT.md)。

## 开始之前

先按[贡献指南](../AGENTS.md#set-up-a-development-environment)建立开发环境，并阅读以下代码：

- [ASR 类型与协议](../framenote/asr/types.py)：后端输入、输出与校验。
- [流水线](../framenote/service_pipeline.py)：音轨准备与进度映射。
- [公共材料发布层](../framenote/materials.py)：原件边界、原子发布和转录导出。
- [SenseVoice 适配器](../framenote/asr/sensevoice.py)：可参考的隔离推理实现。
- [契约测试](../tests/test_asr_contract.py)和[流水线测试](../tests/test_pipeline.py)：合法结果、失败和取消行为。

`Pipeline` 负责规范音轨和编排材料发布。把模型检查、依赖加载、推理协议和原始结果映射封装在后端中。

## 实现并登记后端

按以下步骤接入一个后端：

1. 在 `framenote/asr/` 实现 `ASRBackend` 协议。
   提供 `describe()`、`health()` 和异步 `transcribe()`，返回值须满足本页契约。
2. 在 `service_settings.py` 定义配置、环境变量解析及校验。
   把后端名称加入 `Settings` 的名称校验；路径按启动目录解析，凭据保留在后端配置中。
3. 在 `asr/__init__.py` 的 `create_backend(settings)` 中增加显式分支。
   导入模块时保持轻量，把重型依赖放在适配器或隔离 worker 中加载。
4. 为后端补充契约和流水线测试，覆盖有效时间、静音、错误、进度、取消及原始材料边界。
5. 按[验证接入结果](#验证接入结果)运行检查，再完成真实输入验收。

## 后端接口

`ASRBackend` 定义三个方法：

| 方法 | 返回值 | 调用要求 |
| --- | --- | --- |
| `describe()` | `ASRProvenance` | 返回可公开的模型来源、参数和版本；隐藏凭据与私有路径 |
| `health()` | `BackendHealth` | 返回配置和环境是否就绪，以及不可用原因 |
| `async transcribe(request, *, on_progress, cancelled)` | `ASRResult` | 对规范音轨执行识别；通过回调报告进度并检查取消；失败时抛出 `ASRError`，取消时传播 `asyncio.CancelledError` |

`transcribe()` 的 `request` 为 `ASRRequest`，`on_progress` 接受一个 `ASRProgress`，`cancelled()` 返回是否已请求取消。

### 输入和健康状态

`ASRRequest` 的字段如下：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `audio_path` | `Path` | 本任务目录中的 16 kHz 单声道 FLAC |
| `duration` | `float` | 音轨时长，单位为秒，须为有限正数 |
| `work_dir` | `Path` | 本任务目录，供后端写入原始结果和运行材料 |

`BackendHealth` 的字段如下：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `backend` | `str` | 后端名称 |
| `configured` | `bool` | `true` 表示配置所需条件满足；SenseVoice 实现以必需模型文件齐全为条件 |
| `available` | `bool` | `true` 表示后端就绪；`/api/ready` 据此返回 `200` 或 `503` |
| `reason` | `str` 或 `None` | 不可用原因；可用时可为 `None` |

### 结果和时间区间

`ASRResult` 使用以下字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `segments` | `list[TranscriptSegment]` | 按开始时间排列的识别段落，默认空列表 |
| `timestamp_kind` | `VAD_interval` 或 `segment_interval` | VAD 语音区间或后端原生片段区间 |
| `provenance` | `ASRProvenance` | 模型来源、参数、资产哈希和软件版本 |
| `warnings` | `list[str]` | 可公开的提示，默认空列表 |
| `metrics` | `dict[str, float]` | 实际可用的非负有限数值指标，默认空字典 |
| `raw_path` | `Path` | 已写入本任务目录的原始结果文件 |

`TranscriptSegment` 包含 `start`、`end` 和 `text`。时间相对音轨开头，以秒为单位；`text` 为字符串。每段必须满足以下条件：

- `start` 和 `end` 为有限数值。
- `start` 非递减，且 `0 <= start < min(end, duration)`。
- `end` 最多超出 `duration` 0.1 秒，以容忍编码尾部误差；规范输出把超出部分裁到 `duration`。

静音输入返回空 `segments`；规范材料添加 `no_speech_detected`。仅报告后端实际取得的指标；流水线自行测量转录墙钟时间。

### 来源与原始材料

`ASRProvenance` 的字段如下：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `backend` | `str` | 后端标识 |
| `model` | `str` | 模型标识 |
| `revision` | `str` 或 `None` | 可确认的模型版本；来源不明的自定义权重使用 `None` |
| `parameters` | `dict[str, JSONValue]` | 实际语言、归一化、线程、批量和分段参数，默认空字典 |
| `artifacts` | `dict[str, str]` | 资产名到 SHA-256 的映射，默认空字典 |
| `versions` | `dict[str, str]` | 实际软件版本，默认空字典 |

SenseVoice 的全部默认资产匹配模型锁时，`revision` 使用 `lock-sha256:` 加模型锁内容的 SHA-256。逐文件来源和固定提交见[模型清单](MODELS.md#固定资产)。公开参数使用脱敏标识。

`raw_path` 必须指向本任务目录内真实存在的文件；解析符号链接后仍须满足这个边界。公共材料发布器 `publish_asr` 按原字节发布为 `asr.raw.json`，不依赖原始文件的键名。

ASR 规范转录保持 `schema_version=2`，`ASRResult` 和 `result_to_payload(result, duration=...)` 的契约不变；`raw_artifact` 只保存材料名。公共材料发布层分别接收 ASR 结果和平台字幕，准备 JSON、Markdown、SRT 及原件，发布前后检查取消；调用方在全部输出成功后才把任务标记为完成。

平台字幕使用 transcript v3：`timestamp_kind=subtitle_interval`，`source_kind` 为 `platform_subtitle` 或 `platform_auto_caption`，来源保存为 B站身份、轨道、语言、生成方式、翻译标记、抓取时间和原件 SHA-256。抓取时间未知时为 `null`；空字幕不能作为 ASR 静音结果发布。字幕原件保存为 `subtitle.raw.json`，各候选的 `subtitle-NNNN.raw.json` 和 `subtitle_attempts.json` 继续保留。材料清单版本取任务的 `material_schema_version`，旧任务默认为 2；B站任务清单可以为 v3，而其 ASR 转录仍为 v2。

读取接口兼容 transcript v1、v2 和 v3，v3 校验字幕来源及时间类型，且不修改原文件字节；未知版本和无效段落分别以 `unsupported_material_version` 和 `invalid_transcript` 报错。

## 编写离线示例后端

以下示例与流水线测试中的假后端采用相同接口，用于检查接入路径。示例固定返回“示例”片段；实际后端应返回识别结果：

```python
import asyncio

from framenote.asr.types import ASRProvenance, ASRResult, BackendHealth
from framenote.transcript import TranscriptSegment


class ExampleBackend:
    def describe(self):
        return ASRProvenance(
            backend="example", model="example", revision=None
        )

    def health(self):
        return BackendHealth("example", True, True, None)

    async def transcribe(self, request, *, on_progress, cancelled):
        if cancelled():
            raise asyncio.CancelledError
        raw = request.work_dir / "example.raw.json"
        raw.write_text('{"text":"示例"}', encoding="utf-8")
        if cancelled():
            raise asyncio.CancelledError
        return ASRResult(
            segments=[
                TranscriptSegment(0, min(1, request.duration), "示例")
            ],
            timestamp_kind="segment_interval",
            provenance=self.describe(),
            raw_path=raw,
        )
```

把实例注入 `Pipeline(settings, store, ExampleBackend())`。运行一段有音轨的视频后，规范转录应包含一个 `text="示例"` 的片段，`asr.raw.json` 应与示例写入的原始字节一致。

## 报告进度并处理取消

调用 `on_progress(ASRProgress(completed, total))` 报告阶段内进度。字段含义如下：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `completed` | `int` 或 `None` | 已完成数量 |
| `total` | `int` 或 `None` | 总数量 |
| `message` | `str` | 可选进度说明，默认空字符串；流水线不据此计算进度 |

已知进度要求 `0 <= completed <= total` 且 `total > 0`，两个计数都须为整数。两个值均为 `None` 表示未知进度。流水线把有效计数映射到 30%–95%，无效或未知计数保持最近有效状态；任务完成后才设为 100%。

在异步等待前后和返回结果前检查 `cancelled()`。收到取消时，先清理后端资源，再传播 `asyncio.CancelledError`；任务队列等待清理结束后才取下一项。

执行外部进程时，复用 `service_process.run_process()` 的进程组回收行为：先发送 TERM，最多等待 3 秒，再发送 KILL 并等待进程退出。这样可以处理父进程先退出或清理期间再次取消的情况。

## 返回错误

通过 `ASRError(code, message)` 返回可公开的说明，完整日志保留在任务目录。`code` 使用以下值：

| 错误码 | 含义 |
| --- | --- |
| `asr_unavailable` | 配置、模型、解释器或依赖不可用 |
| `asr_timeout` | 推理超时 |
| `asr_invalid_response` | 后端结果不满足契约 |
| `asr_failed` | 推理执行失败 |

`validate_result(result, duration=...)` 检查结果，违反契约时抛出 `ASRError`。
`result_to_payload(result, duration=...)` 在校验后生成规范 JSON、裁剪尾部时间并补充静音提示。

## 验证接入结果

接入后依次完成以下验证：

1. 运行契约与流水线测试：

   ```bash
   python -m pytest tests/test_asr_contract.py tests/test_pipeline.py -q
   ```

2. 运行类型检查：

   ```bash
   python -m mypy framenote
   ```

3. 在独立 workspace 和端口中处理真实输入，确认时间区间合法、静音结果可读、原始输出保留、取消后可处理下一项。
4. 核对生成的 `transcript.json`、`asr.raw.json` 和 `provenance`，记录实际依赖与模型版本。

完成后，在[配置参考](CONFIGURATION.md)中记录后端配置，并按[原生验收记录](VALIDATION-NATIVE.md)的证据方式记录真实运行结果。
