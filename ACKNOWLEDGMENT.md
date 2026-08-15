# 鸣谢名单 (ACKNOWLEDGMENT.md)

本项目使用了众多优秀的开源软件和工具，在此向所有贡献者和维护者表示衷心感谢！

**项目源码：**
- 仓库：`liuhua520-svg/SVS-Lab-Tools`
- 项目许可证：MIT

本文件覆盖 `backend/requirements.txt`、`backend/requirements-nemo.txt`、
`backend/requirements-qwen3.txt`、`backend/requirements-qwen3tts.txt`
四个依赖清单，以及 `frontend/package.json`。各依赖的许可证信息均已对照
PyPI / npm 官方页面或上游仓库 LICENSE 文件核实，详见文末「最后核实」。
完整的分发合规要求（含 LGPL 依赖的特别说明）见仓库根目录的
`THIRD-PARTY-NOTICES.txt`，本文件是它的表格化摘要版本。

---

## 1. 项目本身

本项目采用 **MIT License** 发布。

Copyright (c) 2026 liuhua520-svg (https://github.com/liuhua520-svg/SVS-Lab-Tools)

详见项目根目录 `LICENSE` 文件。

---

## 2. 后端运行环境说明

本项目的后端拆分为 4 个独立进程/环境运行，以避免包之间的依赖冲突：

| 环境 | 依赖清单 | 服务脚本 | 端口 |
|------|---------|---------|------|
| 主后端 | `backend/requirements.txt` | `app.py` | 5000 |
| Qwen3-ASR / Qwen3-ForcedAligner | `backend/requirements-qwen3.txt` | `qwen3_server.py` | 5001 |
| NVIDIA NeMo 强制对齐 | `backend/requirements-nemo.txt` | `nemo_server.py` | 5002 |
| Qwen3-TTS | `backend/requirements-qwen3tts.txt` | `qwen3tts_server.py` | 5003 |

后三个环境仅在对应功能启用时才需要安装，下表按环境分组列出。

---

## 3. Python 后端依赖

### 3.1 主后端（backend/requirements.txt）

#### MIT License
| 包名 | 版本 | 链接 |
|------|------|------|
| setuptools | 81.0.0 | [GitHub](https://github.com/pypa/setuptools) |
| flask-cors | 4.0.0 | [GitHub](https://github.com/corydolphin/flask-cors) |
| montreal-forced-aligner | 3.3.9 | [GitHub](https://github.com/MontrealCorpusTools/Montreal-Forced-Aligner) |
| textgrid | 1.5 | [GitHub](https://github.com/kylebgorman/textgrid) |
| pypinyin | >0.53.0 | [GitHub](https://github.com/mozillazg/python-pinyin) |
| pycantonese | >5.0.0 | [GitHub](https://github.com/pycantonese/pycantonese) |
| jamo | >0.4.1 | [GitHub](https://github.com/JDongian/python-jamo) |
| pyworld | 0.3.5 | [GitHub](https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder) |
| torchcrepe | 0.0.24 | [GitHub](https://github.com/maxrmorrison/torchcrepe) |
| ctranslate2 | 4.4.0 | [GitHub](https://github.com/OpenNMT/CTranslate2) |
| ruamel.yaml | 0.19.1 | [PyPI](https://pypi.org/project/ruamel.yaml/) |
| mido | 1.3.3 | [GitHub](https://github.com/mido/mido) |

#### BSD-3-Clause
| 包名 | 版本 | 链接 |
|------|------|------|
| numpy | 1.26.4 | [GitHub](https://github.com/numpy/numpy) |
| torch | 2.3.1 (+cpu) | [GitHub](https://github.com/pytorch/pytorch) |
| flask | 2.3.3 | [GitHub](https://github.com/pallets/flask) |
| soundfile | 0.12.1 | [GitHub](https://github.com/bastibe/python-soundfile) |

#### BSD-2-Clause
| 包名 | 版本 | 链接 |
|------|------|------|
| torchaudio | 2.3.1 (+cpu) | [GitHub](https://github.com/pytorch/audio) |
| whisperx | 3.2.0 | [GitHub](https://github.com/m-bain/whisperX) |

#### ISC License
| 包名 | 版本 | 链接 |
|------|------|------|
| librosa | 0.11.0 | [GitHub](https://github.com/librosa/librosa) |
| resampy | 0.4.3 | [GitHub](https://github.com/bmcfee/resampy) |

#### Apache License 2.0
| 包名 | 版本 | 链接 |
|------|------|------|
| requests | >2.34.2 | [GitHub](https://github.com/psf/requests) |
| sudachipy | >0.6.8 | [GitHub](https://github.com/WorksApplications/sudachi) |
| sudachidict-core | >20240409 | [GitHub](https://github.com/WorksApplications/SudachiDict) |
| nltk | >3.10.2 | [GitHub](https://github.com/nltk/nltk) |
| g2p_en | >2.1.0 | [GitHub](https://github.com/Kyubyong/g2p) |
| opencc-python-reimplemented | >0.1.7 | [PyPI](https://pypi.org/project/opencc-python-reimplemented/) |
| transformers | 4.39.3 | [GitHub](https://github.com/huggingface/transformers) |
| tokenizers | 0.15.2 | [GitHub](https://github.com/huggingface/tokenizers) |
| huggingface-hub | 0.36.2 | [GitHub](https://github.com/huggingface/huggingface_hub) |

#### LGPL-2.1-or-later
| 包名 | 版本 | 链接 |
|------|------|------|
| >num2words | 0.5.14 | [GitHub](https://github.com/savoirfairelinux/num2words) |

#### LGPL-3.0（除 `srt_composer.py` 为 MIT）
| 包名 | 版本 | 链接 |
|------|------|------|
| edge-tts | >7.0.0 | [GitHub](https://github.com/rany2/edge-tts) |

> num2words 与 edge-tts 均以未修改的第三方库形式通过 pip 引入，未做二次
> 分发或静态链接；LGPL 合规细节见 `THIRD-PARTY-NOTICES.txt` 第 4 节。

#### PSF-2.0 License（仅 Windows，`sys_platform == "win32"`）
| 包名 | 版本 | 链接 |
|------|------|------|
| pywin32 | >306 | [GitHub](https://github.com/mhammond/pywin32) |

#### 多重许可证（tqdm）
| 包名 | 版本 | 说明 | 链接 |
|------|------|------|------|
| tqdm | >4.70.0 | MIT AND MPL-2.0 | [GitHub](https://github.com/tqdm/tqdm) |

---

### 3.2 Qwen3-ASR / Qwen3-ForcedAligner（backend/requirements-qwen3.txt）

flask / numpy / soundfile / requests / tqdm / torch 与主后端版本一致，
许可证信息见上表，不重复列出。

| 包名 | 版本 | 许可证 | 链接 |
|------|------|--------|------|
| qwen-asr | 0.0.6 | Apache-2.0（包源码；模型权重另行分发，见下方模型说明） | [GitHub](https://github.com/QwenLM/Qwen3-ASR) |
| transformers | 4.57.6 | Apache-2.0（qwen-asr 精确锁定的独立版本，与主后端的 4.39.3 不同） | [GitHub](https://github.com/huggingface/transformers) |
| accelerate | 1.12.0 | Apache-2.0 | [GitHub](https://github.com/huggingface/accelerate) |

---

### 3.3 NVIDIA NeMo 强制对齐（backend/requirements-nemo.txt）

flask / soundfile / requests / tqdm / torch / torchaudio 许可证信息见上表。

| 包名 | 版本 | 许可证 | 链接 |
|------|------|--------|------|
| nemo_toolkit[asr] | 2.7.3 | Apache-2.0 | [GitHub](https://github.com/NVIDIA-NeMo/Speech) |

> nemo_toolkit 依赖树较大（packaging / fsspec / omegaconf / hydra-core /
> lightning 等），因此单独隔离到独立环境，不与主后端共用；下载的预训练
> 模型及 NVIDIA 运行时组件遵循各自独立的许可证。

---

### 3.4 Qwen3-TTS（backend/requirements-qwen3tts.txt）

flask / numpy / soundfile / requests / tqdm / torch / torchaudio 许可证
信息见上表。

| 包名 | 版本 | 许可证 | 链接 |
|------|------|--------|------|
| torchvision | 0.18.1 (+cpu) | BSD-3-Clause | [GitHub](https://github.com/pytorch/vision) |
| qwen-tts | 0.1.1 | Apache-2.0（包源码；模型权重另行分发，见下方模型说明） | [GitHub](https://github.com/QwenLM/Qwen3-TTS) |
| transformers | 4.57.3 | Apache-2.0（qwen-tts 精确锁定的独立版本，与 qwen-asr 的 4.57.6 不同） | [GitHub](https://github.com/huggingface/transformers) |
| accelerate | 1.12.0 | Apache-2.0 | [GitHub](https://github.com/huggingface/accelerate) |

---

### 3.5 运行期下载的模型 / 词典 / 语料（不随本仓库分发）

以下资源由本项目各组件在运行时自动下载，**不包含在本仓库源码中**，也
**不受本项目 MIT 许可证覆盖**，请分发前分别确认其各自许可证：

- Montreal Forced Aligner 声学模型与发音词典（`mfa model download` 下载）
- WhisperX 对齐 / 识别模型
- Qwen3-ASR-1.7B / Qwen3-ASR-0.6B 模型权重（Apache-2.0，Alibaba Qwen Team）
- Qwen3-ForcedAligner-0.6B 模型权重（Apache-2.0，Alibaba Qwen Team）
- Qwen3-TTS-12Hz CustomVoice / VoiceDesign / Base 模型权重及
  Qwen3-TTS-Tokenizer-12Hz（Apache-2.0，Alibaba Qwen Team）
- NVIDIA NeMo 强制对齐预训练模型
- nltk 通过 `nltk.download()` 拉取的语料/分词数据
- Microsoft Edge 在线语音（EdgeTTS）与 Windows SAPI5 语音（"讲述人"
  引擎）均为云端/系统服务，不随本项目分发

---

## 4. 前端依赖 (frontend/package.json)

#### MIT License
| 包名                        | 版本          | 链接 |
|----------------------------|---------------|------|
| vue                        | ^3.3.4        | [GitHub](https://github.com/vuejs/core) |
| element-plus               | ^2.4.1        | [GitHub](https://github.com/element-plus/element-plus) |
| @element-plus/icons-vue    | ^2.1.0        | [GitHub](https://github.com/element-plus/element-plus) |
| axios                      | ^1.5.0        | [GitHub](https://github.com/axios/axios) |
| vue-i18n                   | ^11.4.6       | [GitHub](https://github.com/intlify/vue-i18n) |
| @vitejs/plugin-vue         | ^4.3.4        | [GitHub](https://github.com/vitejs/vite-plugin-vue) |
| @vue/tsconfig              | ^0.4.0        | [GitHub](https://github.com/vuejs/tsconfig) |
| vite                       | ^4.4.9        | [GitHub](https://github.com/vitejs/vite) |
| vue-tsc                    | ^1.8.13       | [GitHub](https://github.com/vuejs/language-tools) |

#### BSD-2-Clause
| 包名                        | 版本          | 链接 |
|----------------------------|---------------|------|
| terser                     | ^5.29.1       | [GitHub](https://github.com/terser/terser) |

#### Apache License 2.0
| 包名                        | 版本          | 链接 |
|----------------------------|---------------|------|
| typescript                 | ^5.1.6        | [GitHub](https://github.com/microsoft/TypeScript) |

---

## 5. 特别感谢

- **所有开源项目的开发者与维护者**，没有你们就没有这个工具。
- **MFA、WhisperX、Qwen3-ASR、Qwen3-TTS、NVIDIA NeMo** 等语音工具的作者。
- **PyTorch、Vite、Element Plus** 等基础框架的贡献者。

---

## 6. 使用说明

当您分发本项目时，请：
- 保留本 `ACKNOWLEDGMENT.md` 与仓库根目录 `THIRD-PARTY-NOTICES.txt` 文件
- 保留项目 `LICENSE` 文件
- 如有修改依赖，请同步更新本文件（含新增/移除的依赖及其许可证）
- 遵守各依赖的许可证条款；LGPL 依赖（num2words、edge-tts）的具体合规
  方式见 `THIRD-PARTY-NOTICES.txt` 第 4 节
- 根据 Apache License 2.0 要求，如果依赖自带 NOTICE 文件则必须一并保留
- 如分发运行期下载的模型/词典/语料（见 3.5 节），请遵守其各自许可证

**上表列出的依赖许可证均为宽松型（MIT / BSD / Apache-2.0 / ISC / PSF-2.0
等），在满足署名与许可证声明的前提下通常允许商业使用与闭源集成；
num2words、edge-tts 两个 LGPL 依赖额外要求保留用户单独升级/替换该库的
能力，不要求本项目自身源码采用相同许可证发布。**

---

**最后核实**：2026-08-13（对照 PyPI / npm 官方页面及上游仓库 LICENSE 文件逐项核实）
