# 模型准备与核验

在安装 FrameNote 的 Python 环境中运行；命令使用标准库，默认清单是随包分发的资源，与启动目录无关。

```bash
python -m framenote.models prepare --out models
python -m framenote.models verify --out models
```

prepare 从下表公开仓库的固定提交下载全部 10 个文件。每个文件先保存到同目录的临时 .part，核对字节数与 SHA-256 后原子替换；已匹配文件复用，下载失败保留原有文件并清理临时文件。每个下载的超时为 120 秒。失败时退出码为 1，并指出目标文件；重跑 prepare 可继续准备缺失/损坏资产。

verify 完全离线，逐个检查所有文件并汇总缺项。可将整个 models/ 复制到另一机器，再运行 verify。服务配置分别指向 models/sensevoice 和 models/vad；服务启动的轻量文件检查不代替准备阶段的完整哈希核验。

自定义清单可使用 `--manifest /path/to/models.lock.json`。清单格式与默认包资源一致：schema_version=1，每项含 target、repo、40 位固定 revision、url、bytes、sha256、license_url；目标仅可位于输出目录中的 sensevoice/ 或 vad/，不能借 symlink 越界。默认资源见 [models.lock.json](../framenote/asr/models.lock.json)。

## 固定资产

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

清单中的 URL 为 ModelScope 对应仓库的 resolve/<revision>/<file> 路径；BPE 来自独立的 iic/SenseVoiceSmall 仓库。所有 10 个资产已在本轮从公开源重新下载并通过完整校验。

## 来源与许可

FrameNote 代码沿用项目 [LICENSE](../LICENSE) 与 [NOTICE](../NOTICE) 的 MIT 及上游归属。运行依赖保留各自发行包的许可，安装时可通过包元数据查询；模型文件按其上游来源和模型许可管理。

模型来源：[SenseVoice ONNX](https://modelscope.cn/models/iic/SenseVoiceSmall-onnx)、[SenseVoice/BPE](https://modelscope.cn/models/iic/SenseVoiceSmall)、[FSMN VAD ONNX](https://modelscope.cn/models/damo/speech_fsmn_vad_zh-cn-16k-common-onnx)。模型卡链接单独的 [FunASR MODEL_LICENSE](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)；[SenseVoice 模型卡](https://huggingface.co/FunAudioLLM/SenseVoiceSmall/blob/main/README.md) 和 [ONNX 运行代码](https://github.com/modelscope/FunASR/tree/main/runtime/python/onnxruntime) 分别说明模型与代码来源。发行包保存固定下载清单和许可来源链接，权重由准备命令从上游取得。
