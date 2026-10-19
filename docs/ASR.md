# ASR 契约与开发者扩展

当前工厂交付 SenseVoiceSmall ONNX INT8 + FSMN VAD。Pipeline 只处理规范音轨、ASRBackend 和材料；新增后端的协议与依赖封装在具体实现中。

## 四步接入

1. 在 framenote/asr/ 实现 ASRBackend：describe() 返回脱敏来源，health() 返回配置/环境状态，async transcribe(request, *, on_progress, cancelled) 返回 ASRResult。
2. 在 service_settings.py 定义后端配置、环境变量解析和校验，并将新名称加入 Settings 的已登记名称校验。路径以启动目录解析，凭据留在具体配置内。
3. 在 asr/__init__.py 的 create_backend(settings) 增加显式分支，返回该实现。模块导入保持轻量，重型依赖在适配器/隔离 worker 内加载。
4. 运行 tests/test_asr_contract.py，并像 tests/test_pipeline.py 一样注入后端，验证合法时间、静音、错误、取消、进度和原始材料边界；再完成真实输入验收。

## 输入与结果

ASRRequest.audio_path 是任务目录中的 16 kHz 单声道 FLAC；duration 为音轨时长（秒），work_dir 是任务目录。ASRResult 的每段包含 start/end/text，区间相对音轨开头，start 非递减且 0<=start<end；尾部最多容忍 0.1 秒编码偏差，规范输出会裁到 duration。

timestamp_kind 为 VAD_interval 或 segment_interval，分别说明 VAD 区间和后端原生片段区间。静音返回空 segments，规范材料加入 no_speech_detected。metrics 可为空，仅收录实际可用的非负有限值。Pipeline 自行计算转录墙钟时间。

ASRProvenance 包含 backend/model/revision、parameters、artifacts（材料/权重名到 SHA-256）和 versions。无法确认来源的自定义权重 revision=None；默认资产全部匹配时，SenseVoice 的 revision 是模型锁内容的 SHA-256 标识，逐文件仓库和固定提交见包内锁。parameters 保存实际语言、归一化、线程、批量与分段参数，公开输出使用脱敏标识。

raw_path 必须是真实存在且位于本任务目录内的文件（解析 symlink 后也须满足）；Pipeline 以 asr.raw.json 发布原字节。规范 JSON 为 schema_version=2，raw_artifact 只使用材料名。未知材料版本与坏段落由读取 API 明确报错；v1 读取不修改原文件。

## 最小离线后端

下面的形式与 tests/test_pipeline.py 的 FakeBackend 相同，可用于验证接缝。此示例生成已知片段，正式后端应返回真实识别内容。

```python
from framenote.asr.types import ASRProvenance, ASRResult, BackendHealth
from framenote.transcript import TranscriptSegment

class ExampleBackend:
    def describe(self):
        return ASRProvenance(backend="example", model="example", revision=None)

    def health(self):
        return BackendHealth("example", True, True, None)

    async def transcribe(self, request, *, on_progress, cancelled):
        import asyncio
        if cancelled():
            raise asyncio.CancelledError
        raw = request.work_dir / "example.raw.json"
        raw.write_text('{"text":"示例"}', encoding="utf-8")
        return ASRResult(
            segments=[TranscriptSegment(0, min(1, request.duration), "示例")],
            timestamp_kind="segment_interval", provenance=self.describe(), raw_path=raw,
        )
```

注入 Pipeline(settings, store, ExampleBackend()) 后，流水线不需要读取示例 raw 的键名或模型指标。

## 进度、取消与错误

通过 on_progress(ASRProgress(completed,total)) 报告阶段内计数；两者同时为 None 表示未知进度。有效计数满足 0<=completed<=total 且 total>0，Pipeline 映射到30–95%；畸形计数保持最近有效状态。任务完成后才置100%。

取消以 asyncio.CancelledError 传播，等待前后和返回前检查 cancelled()。清理后再返回/抛出，Jobs 才会取下一任务。现有 run_process 负责整个进程组，先 TERM、最多等待3秒再 KILL；复用它以保留后代回收行为。

ASRError 的 code 为 asr_unavailable、asr_timeout、asr_invalid_response、asr_failed，分别代表环境、超时、结果契约与执行失败。构造公开错误时使用可公开的说明；完整运行日志保存在本地任务目录。部署与模型准备见 [配置](CONFIGURATION.md)、[模型](MODELS.md) 和 [部署](DEPLOYMENT.md)。
