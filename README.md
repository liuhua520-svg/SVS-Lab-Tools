# SVS Lab Tools

**Singing Voice Synthesis Lab Tools**

一个完全独立的 Web 应用程序，面向歌声合成（SVS）工作流，用于把“输入音频 + 文本”转换为可在歌声合成软件中使用的工程文件。

它的目标不是做 SVS，也不是替代歌声合成引擎本身，而是作为一座桥梁，帮助歌姬“假装擅长说话”：

- 先用 **[Qwen3-ForcedAligner-0.6B](https://github.com/QwenLM/Qwen3-ASR)** 将音频与文本自动对齐，生成 `.lab`
- 再用 **[PyWORLD](https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder)** 提取 F0 音高
- 最后输出歌声合成工程文件，便于在软件中继续编辑和播放

---

## 语言（Language）

[**简体中文**](./README.md) | [**繁體中文**](./Docs/README/README_ZH_TW.md) | [**English**](./Docs/README/README_EN.md) | [**日本語**](./Docs/README/README_JP.md) | [**한국어**](./Docs/README/README_KR.md) 

---

## ✨ 主要功能

* **仅标注（快速）**
  只执行 Qwen3-ForcedAligner-0.6B 自动对齐，快速生成 `.lab`。

* **完整处理（标注 + F0 + 工程文件）**
  自动完成标注、音高提取，并输出歌声合成工程文件。

* **仅生成工程（WAV + LAB）**
  基于已有的 `WAV + LAB/MIDI` 直接生成工程文件，无需重新标注。

* **支持多种音高提取算法**
  内置 **PyWORLD、CREPE、RMVPE** 三种 F0 提取方式，可根据不同需求选择不同算法。

* **支持连续音高写入**
  可选择仅生成纯净音符，也可将连续 F0 曲线写入工程，保留更加真实的音高变化。

* **支持长音频处理**
  能够处理长时间音频，适用于完整歌曲、长篇语音等场景。

* **支持多种文本转换方式**
  在「仅生成工程（WAV+LAB/MIDI）」模式下，可选择：

  * 不转换
  * 合并辅音
  * 平假名
  * 片假名

* **支持多种音频格式**
  `WAV / MP3 / FLAC / M4A / AAC / OGG`

* **支持多语言文本对齐**

* **支持 LAB 文件导出**

* **支持多种对齐算法**
  内置 **Montreal Forced Aligner、WhisperX、Qwen3-ASR-1.7B、Qwen3-ForcedAligner-0.6B、NeMo Forced Aligner** 五种对齐方式，可根据不同需求选择不同算法。
  
* **提供简洁易用的 Web 界面**

* **支持批量处理多个 LAB / MIDI / 文本 + WAV 转换成工程文件**

---

## 📌 适用场景

- 已经有歌声合成或 TTS 音频，希望快速生成标注
- 想把语音工程导入歌声合成软件继续编辑
- 想批量生成 `.lab`、F0 和工程文件
- 想在“说话”和“唱歌”之间建立可编辑的中间层

---

## 🛠️ 工作原理

1. 输入音频文件与文本
2. 使用 **[Qwen3-ForcedAligner-0.6B](https://github.com/QwenLM/Qwen3-ASR)** 进行自动时间对齐
3. 生成 `.lab` 标注文件
4. 使用 **[PyWORLD](https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder)** 提取 F0 音高
5. 根据处理模式输出工程文件
6. 将结果导入歌声合成软件继续使用

---

## 🚀 快速开始

### Windows

```batch
setup.bat
run.bat
```

### Linux / Mac

```Linux / Mac
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

启动后访问：
```
http://127.0.0.1:5000
```
## 🔧 处理模式
### 1. 仅标注（快速）

只进行 Qwen3-FA 自动标注，生成 .lab 文件。

### 2. 完整处理（标注 + F0 + 工程文件）

执行 Qwen3-FA 对齐、F0 提取，并生成工程文件。

### 3. 仅生成工程（WAV + LAB）

使用已有的 WAV + LAB 生成工程文件，不再重复做完整标注流程。

### 📋 系统要求
Python 3.8+

Node.js 16+

4GB RAM

1GB 可用磁盘空间

### 🔧 安装 MFA
```
pip install montreal-forced-aligner
```

# 下载 MFA 语言模型
```
mfa model download acoustic cmn  # 中文
mfa model download acoustic eng  # 英语
mfa model download acoustic jpn  # 日语
mfa model download acoustic kor  # 韩语
mfa model download acoustic yue  # 粤语
```

## 📖 使用流程
### 仅标注（快速）

1. 打开本应用

2. 上传音频文件

3. 输入文本

4. 选择语言

5. 点击开始标注

6. 下载生成的 .lab

### 完整处理（标注 + F0 + 工程文件）

1. 上传音频文件

2. 输入文本

3. 选择语言

4. 选择处理模式为“完整处理”

5. 点击开始处理

6. 下载生成的工程文件

### 仅生成工程（WAV + LAB）

1. 准备好 WAV 和对应的 LAB

2. 选择“仅生成工程”

3. 上传 WAV + LAB

4. 点击生成

5. 下载工程文件

### 📝 输出内容
根据处理模式，应用可输出：

- .lab 时间对齐文件
- F0 音高信息
- 歌声合成工程文件

### 📁 目录结构
```
backend/    后端服务
frontend/   前端界面
setup.bat   Windows 环境安装脚本
run.bat     Windows 启动脚本
setup.sh    Linux / Mac 环境安装脚本
run.sh      Linux / Mac 启动脚本
```

## ⚠️ 免责声明

本项目用于以下用途：

- 音频自动对齐（Qwen3-FA）
- 音高提取（PyWORLD / F0）
- 歌声合成工程文件生成

---

### 📌 功能限制

本工具仅作为“SVS 工作流辅助工具”，不具备以下能力：

- 不进行歌声合成（SVS）
- 不训练或修改声库模型
- 不替代任何歌声合成软件

---

### 📁 使用责任

使用本工具生成的结果（包括 `.lab`、F0 数据及工程文件）：

- 由使用者自行负责其合法性与用途
- 使用前请确认所用音频及声库的授权范围
- 若涉及第三方声库，请遵守其官方使用条款

---

### 🎭 声库与标注说明

如声库或合成系统要求注明来源，请根据对应协议标注（是否要标明演技指导取决于转换成工程文件后的声库的声音是否被识别有原样本特征，如果被识别有原样本特征，请根据对应协议标注），例如：

- “使用声库：XXX”
- “演技指导：XXX”

---

### 📌 版权说明

本项目不声明对用户输入数据或生成内容的所有权。

数据版权归使用者所有，具体取决于所使用原样本转换成工程文件后的声库的声音，是否被识别有原样本特征。如果使用第三方原样本被识别有原样本特征，则可能被视为第三方的衍生作品。

---

## 📝 许可证
本项目源代码采用 MIT License。

除另有说明外，本项目中由作者原创的非软件内容（如原创文档、原创示例等）同样采用 MIT License。

第三方模型、字典、声库、数据集及其衍生文件仍遵循各自许可证，本项目的 MIT License 不改变其授权方式。
