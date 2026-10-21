# 准备和核验模型

本指南供 FrameNote 部署者使用，说明如何下载固定版本的模型、离线核验完整性，以及查找资产来源和许可。命令使用 Python 标准库；在已安装 FrameNote 的环境中执行。

## 准备并核验模型

确保运行目录可写，且下载模型的机器能访问锁文件中的公开来源，然后完成以下步骤：

1. 下载模型：

   ```bash
   python -m framenote.models prepare --out models
   ```

   命令下载固定清单中的 10 个文件，复用已匹配的文件。成功时输出：

   ```text
   Verified all model assets
   ```

2. 离线核验全部文件：

   ```bash
   python -m framenote.models verify --out models
   ```

   成功时同样输出 `Verified all model assets`。失败时退出码为 `1`，错误消息列出缺失或损坏的文件。

3. 将服务的模型路径指向 `models/sensevoice` 和 `models/vad`。
   使用其他输出目录时，同步调整 `FRAMENOTE_SENSEVOICE_MODEL` 和 `FRAMENOTE_VAD_MODEL`，参见[服务端配置](CONFIGURATION.md#服务端配置)。

`prepare` 先下载到目标文件同目录的临时 `.part` 文件，核对字节数和 SHA-256 后再原子替换。每个文件的下载时限为 120 秒。失败时保留原有文件并清理临时文件；重新执行 `prepare` 可继续处理缺失或损坏的资产。

`verify` 完全离线。把整个 `models/` 复制到另一台机器后，在目标机器重新执行核验。服务的就绪检查只确认文件存在及依赖可用，完整哈希检查由 `verify` 完成。

## 使用自定义清单

默认清单是随包分发的 [models.lock.json](../framenote/asr/models.lock.json)，与执行目录无关。需要使用自定义资产时，在两个命令中传入相同的清单：

```bash
python -m framenote.models prepare --out models --manifest 'MANIFEST_PATH'
python -m framenote.models verify --out models --manifest 'MANIFEST_PATH'
```

将 `MANIFEST_PATH` 替换为自定义 `models.lock.json` 的路径。确认两个命令均输出 `Verified all model assets`。

清单顶层为 `schema_version=1`，`files` 为非空数组。每个资产包含以下字段：

| 字段 | 要求 |
| --- | --- |
| `target` | 唯一的相对路径，形式为 `sensevoice/FILENAME` 或 `vad/FILENAME`，其中 `FILENAME` 是文件名；解析符号链接后仍位于输出目录内 |
| `repo` | 非空的来源仓库名 |
| `revision` | 40 位小写十六进制固定提交标识 |
| `url` | 下载文件的 HTTP 或 HTTPS URL，不含用户名或密码 |
| `bytes` | 文件字节数，正整数 |
| `sha256` | 64 位小写十六进制 SHA-256 |
| `license_url` | 许可来源的 HTTP 或 HTTPS URL，不含用户名或密码 |

## 固定资产

默认锁文件记录的资产、提交和哈希如下。下载 URL 使用 ModelScope 仓库的固定提交路径，BPE 分词模型来自独立的 `iic/SenseVoiceSmall` 仓库：

| 目标文件 | 公开仓库 | 文件版本 | SHA-256 |
| --- | --- | --- | --- |
| sensevoice/am.mvn | iic/SenseVoiceSmall-onnx | 4e95991ddb34c70e9b94f026d62ac82a9d941ae1 | 29b3c740a2c0cfc6b308126d31d7f265fa2be74f3bb095cd2f143ea970896ae5 |
| sensevoice/config.yaml | iic/SenseVoiceSmall-onnx | 4e95991ddb34c70e9b94f026d62ac82a9d941ae1 | f71e239ba36705564b5bf2d2ffd07eece07b8e3f2bbf6d2c99d8df856339ac19 |
| sensevoice/configuration.json | iic/SenseVoiceSmall-onnx | 000ca612d24c796072270fdffb15b7f4e0f27926 | c57f6a580d63f7465c6a22ba95847aee05a1ae1181f5abddffb943d9febda061 |
| sensevoice/model_quant.onnx | iic/SenseVoiceSmall-onnx | 4e95991ddb34c70e9b94f026d62ac82a9d941ae1 | 21dc965f689a78d1604717bf561e40d5a236087c85a95584567835750549e822 |
| sensevoice/tokens.json | iic/SenseVoiceSmall-onnx | 4e95991ddb34c70e9b94f026d62ac82a9d941ae1 | a2594fc1474e78973149cba8cd1f603ebed8c39c7decb470631f66e70ce58e97 |
| sensevoice/chn_jpn_yue_eng_ko_spectok.bpe.model | iic/SenseVoiceSmall | 70514a3da51f1160f51d18449dab6128bbd4928b | aa87f86064c3730d799ddf7af3c04659151102cba548bce325cf06ba4da4e6a8 |
| vad/am.mvn | damo/speech_fsmn_vad_zh-cn-16k-common-onnx | 9e08ccbfe15c79d0f4b41247c5ed4266031ac362 | 6820fef9687708c4fc3fab2530179c8fcea6262daa25514380056cd8f6eb1754 |
| vad/config.yaml | damo/speech_fsmn_vad_zh-cn-16k-common-onnx | a158155ef9e81f405c052f40b6d1ad43b87e6215 | 2ef334f2d7776edd86ed696296774f87550206c256bfabc597872ad861831589 |
| vad/configuration.json | damo/speech_fsmn_vad_zh-cn-16k-common-onnx | bdaa7c49489f31282c1c17a3b86e2c80e40f8d30 | f62fcbcebbaade798714b34c521cad87d869ee6a327ceed948e01ff2336bbfc0 |
| vad/model_quant.onnx | damo/speech_fsmn_vad_zh-cn-16k-common-onnx | f436483dfd1bf3eb1b410133ff86ecc9e10abc4a | 5289eb2aa3c9af2d7a4284bcfa7c3ceb81d360814ed4203239b6c5d0569da8a1 |

默认 10 个资产的独立下载与完整核验结果记录在[2026-10-20 原生部署验收](VALIDATION-NATIVE.md#安装与模型)。该记录描述对应版本的验收，实际部署仍需执行核验。

## 来源与许可

FrameNote 代码的 MIT 许可和上游归属见 [LICENSE](../LICENSE) 与 [NOTICE](../NOTICE)。运行依赖保留各自发行包的许可，可从包元数据查询；模型资产按各自上游来源和模型许可管理。

模型与运行代码的来源如下：

- [SenseVoice ONNX 模型](https://modelscope.cn/models/iic/SenseVoiceSmall-onnx)。
- [SenseVoice 与 BPE 模型](https://modelscope.cn/models/iic/SenseVoiceSmall)。
- [FSMN VAD ONNX 模型](https://modelscope.cn/models/damo/speech_fsmn_vad_zh-cn-16k-common-onnx)。
- [FunASR MODEL_LICENSE](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)，模型清单保留的许可来源。
- [SenseVoice 模型卡](https://huggingface.co/FunAudioLLM/SenseVoiceSmall/blob/main/README.md)。
- [FunASR ONNX 运行代码](https://github.com/modelscope/FunASR/tree/main/runtime/python/onnxruntime)。

发行包保存固定下载清单与许可链接，权重由准备命令从上游下载。模型核验通过后，按[启动并验证服务](DEPLOYMENT.md#启动并验证服务)继续部署。
