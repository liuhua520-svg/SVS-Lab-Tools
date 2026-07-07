# NeMo Forced Aligner 集成说明

参考实现：https://github.com/NVIDIA-NeMo/Speech/tree/main/tools/nemo_forced_aligner

## 改动的文件

| 文件 | 改动 |
|---|---|
| `alt_aligners.py` | 新增 `NeMoForcedAligner` 类 + `_NEMO_CACHE` 缓存目录 + 工厂函数 `get_aligner()`/`get_alt_aligner_status()` 支持 `"nemo_aligner"` |
| `pipeline.py` | `_run_alignment()` / `process_full()` / `process_mfa_only()` 新增 `nemo_model` 可选参数 |
| `app.py` | 两处 `aligner_backend` 白名单加入 `"nemo_aligner"`；两个 Flask 端点新增 `nemo_model` 表单字段透传 |
| `MFAProcessor.vue` | 新增 NeMo 单选项、设备提示、安装提示、状态表格标签修复 |
| `i18n.ts` | 5 个语言区块（zh-CN/zh-TW/en/ja/ko）各新增 4 个翻译键 |

## 设计取舍：为什么不是 manifest + align.py

官方 NFA 是一个 **批处理 CLI 工具**（Hydra config + manifest JSON + 落盘 CTM/ASS），
而这个项目里 WhisperX / Qwen3-ASR / Qwen3-ForcedAligner 三个现有后端都是
**进程内单文件调用**模式（Flask 收到一个文件 → 直接调模型 → 直接返回 LAB）。

`NeMoForcedAligner` 选择贴合现有后端的调用约定，而不是按官方 CLI 的方式去写
manifest、起子进程、解析 CTM 文件。两者内部算法完全一致：

1. 加载一个 NeMo **CTC 模型**，或 CTC 模式下的 **Hybrid CTC-Transducer 模型**
   （`change_decoding_strategy(decoder_type="ctc")`）
2. 对音频做一次前向传播，拿到逐帧 log-probs
3. 用 `torchaudio.functional.forced_align`（Viterbi 解码）把参考文本的 token 序列
   和 log-probs 对齐
4. 转成 word_entries，交给本项目已有的 `AltAlignerBase._word_entries_to_lab()`
   （和 WhisperX / Qwen3-FA 共用同一套中/英/日/韩 LAB 生成与静音补全逻辑）

这是官方文档本身也确认的机制（NFA 内部就是 CTC log-probs + Viterbi forced
alignment），所以效果上是等价的，只是省去了 manifest/CTM 文件落盘这一层。

## 模型选择：只用满足 NFA 限制的官方 checkpoint

NVIDIA 官方文档明确写道：**NFA 只能用 CTC 模型，或 CTC 模式下的 Hybrid
CTC-Transducer 模型；纯 Transducer/TDT 模型不能直接用于强制对齐**。

因此 `NeMoForcedAligner.LANGUAGE_MODELS` 只收录满足这一限制的官方模型：

| 语言 | 模型 | 类型 | 来源 |
|---|---|---|---|
| en | `stt_en_fastconformer_hybrid_large_pc` | Hybrid CTC+RNNT（走 CTC 模式） | NGC |
| zh | `nvidia/stt_zh_citrinet_1024_gamma_0_25` | 纯 CTC（字符级） | HuggingFace Hub |
| ja | `nvidia/parakeet-tdt_ctc-0.6b-ja` | Hybrid TDT+CTC（走 CTC 模式） | HuggingFace Hub |

**韩语 / 粤语目前没有提供默认模型**：NVIDIA 没有发布这两个语言的官方
CTC 或 Hybrid-CTC checkpoint（韩语只有社区的纯 Transducer 模型
`eesungkim/stt_kr_conformer_transducer_large`，不满足 NFA 的解码器类型限制）。
选择这两个语言时 `align()` 会抛出清晰的报错，提示改用 WhisperX /
Qwen3-ASR / Qwen3-ForcedAligner，而不是静默用一个不兼容的模型类型硬跑。

如果之后 NVIDIA 发布了合适的 checkpoint，或你有自训练的 CTC 模型，可以通过
环境变量指定，不需要改代码：

```bash
set NEMO_FA_MODEL_KO=your_org/your_korean_ctc_model
set NEMO_FA_MODEL_YUE=your_org/your_cantonese_ctc_model
```

## 安装

```bash
# 在 .mfa_env（或你跑 alt_aligners.py 的那个 conda 环境）里：
pip install "nemo_toolkit[asr]"
```

`requirements.txt` 未自动加入这一行——`nemo_toolkit` 体积较大（连同
PyTorch Lightning、Hydra、OmegaConf 等一长串依赖），这与 `qwen-asr` /
`whisperx` 一样，按需安装，避免不用 NeMo 的人也被迫装一遍。如果你希望我把它
也写进 `requirements.txt`（带注释说明是可选依赖），告诉我一声即可。

## 前端

`MFAProcessor.vue` 的对齐后端选择器里新增了"NeMo-FA"单选项，行为与
Qwen3-ForcedAligner 一致：**需要参考文本**，不支持纯 ASR 模式，
project-only 模式下会被自动切回 mfa-only（沿用现有逻辑）。

状态徽章、安装提示、设备提示（GPU 模式下提示首次会自动下载模型）均已补全，
5 个界面语言（简中/繁中/英/日/韩）都已同步翻译。

## 已知限制

- 没有暴露模型选择的 UI 下拉框（跟 Qwen3-FA 一样零额外参数），如果需要可以
  加一个类似 WhisperX 模型选择器的 `nemo_model` 输入框，表单字段已经在
  后端打通（`nemo_model` 留空即用语言默认模型）。
- `_tokenize()` 对 BPE tokenizer 的 token→文本转换做了多层兜底（不同
  NeMo 版本的 tokenizer API 不完全一致），如果某个特定模型版本的
  `ids_to_tokens`/`id_to_piece` 行为有出入，请把报错信息发给我，
  我可以针对性修一下。
- 韩语 / 粤语暂不可用（见上文模型选择部分），这是模型可用性的客观限制，
  不是代码 bug。
