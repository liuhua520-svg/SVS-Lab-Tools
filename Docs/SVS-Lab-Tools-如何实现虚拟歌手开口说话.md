# SVS Lab Tools 是如何实现 Synthesizer V、OpenUTAU、VOCALOID 虚拟歌手开口说话的？

> 本报告基于对 `backend/mfa_processor.py`、`backend/alt_aligners.py`、
> `backend/phoneme_converter.py`、`backend/tsubaki_processor.py`、
> `backend/f0_extractors.py`、`backend/midi_processor.py`、`backend/pipeline.py`
> 等源码的直接阅读整理而成，只回答一个具体问题：**一段"音频 + 文本"是怎么一步步
> 变成 Synthesizer V / OpenUTAU / VOCALOID4 打开就能直接播放、虚拟歌手已经在
> "开口唱"的工程文件的？** 涉及的函数名、字段名、常量均对应仓库当前源码，可按文中
> 标注的文件位置直接查证。

---

## 目录

1. [先厘清一个概念：SVS Lab Tools 不合成声音](#1-先厘清一个概念svs-lab-tools-不合成声音)
2. [三家引擎"听懂"的到底是什么](#2-三家引擎听懂的到底是什么)
3. [全链路总览：四步管线](#3-全链路总览四步管线)
4. [第一步：强制对齐 —— 拿到"什么时候发什么音"](#4-第一步强制对齐--拿到什么时候发什么音)
5. [第二步：音素翻译 —— 把 MFA/ASR 的输出改写成引擎认识的符号](#5-第二步音素翻译--把-mfaasr-的输出改写成引擎认识的符号)
6. [第三步：音高提取 —— 给每个音符一个"该唱多高"](#6-第三步音高提取--给每个音符一个该唱多高)
7. [第四步：工程文件序列化 —— 三种格式各自的"发声契约"](#7-第四步工程文件序列化--三种格式各自的发声契约)
8. [完整案例：一句日语歌词如何变成 VOCALOID4 能唱的样子](#8-完整案例一句日语歌词如何变成-vocaloid4-能唱的样子)
9. [多轨/对话批处理：多段音频拼成一个工程](#9-多轨对话批处理多段音频拼成一个工程)
10. [MIDI 输入路径：跳过 F0 判定，直接借音符音高](#10-midi-输入路径跳过-f0-判定直接借音符音高)
11. [这套系统的边界：它做的和不做的](#11-这套系统的边界它做的和不做的)
12. [关键代码位置速查表](#12-关键代码位置速查表)

---

## 1. 先厘清一个概念：SVS Lab Tools 不合成声音

Synthesizer V、OpenUTAU、VOCALOID4 各自内置了完整的歌声合成引擎（声库 + 声学模型/
拼接算法），"虚拟歌手怎么发出声音"这件事完全是这三款软件自己的事，SVS Lab Tools
从不参与、也无法参与。

SVS Lab Tools 真正做的事是更上游的一步：**把一段"人唱/人念的参考音频 + 对应歌词
文本"，翻译成这三款软件能够读取、并且会严格按照其中的时间戳和音高去驱动虚拟歌手
"唱出跟参考音频节奏、发音高度一致"的工程文件**。类比一下：

```
SVS Lab Tools 干的事    ≈  给虚拟歌手写"精确到毫秒的乐谱 + 逐字注音"
Synthesizer V / OpenUTAU / VOCALOID4 干的事  ≈  照着这份乐谱 + 注音真正"唱"出来
```

工程文件里没有一个字节是音频波形——从头到尾都是**音符（起止时间 + 音高）+
歌词/音素（每个音符该发什么音）+ 音高曲线（音符内部的微调）**这三类结构化数据。
本报告要讲清楚的，就是这三类数据分别是怎么算出来的。

---

## 2. 三家引擎"听懂"的到底是什么

在深入管线之前，先弄清楚三个目标格式各自要求"喂"给它们什么，这决定了后面每一步
的输出必须长成什么样：

| 引擎 | 工程文件后缀 | 时间轴单位 | 音符怎么"发声" | 音高怎么"弯曲" |
|---|---|---|---|---|
| Synthesizer V | `.svp` | **blick**（与 BPM 相关的整数刻度，非物理"秒"） | 每个音符自带 `phonemes` 字段（不填则用软件自带 G2P），JSON 结构 | `pitchDelta` 参数轨道，cubic 曲线，单位 cents |
| OpenUTAU | `.ustx` | **tick**（480 tick/四分音符，随 BPM 换算） | 音符歌词若是罗马字/假名，走 UTAU 声库自带的音素拆分；`+` 表示延音 | 音轨级 `curves` 列表里的 `pitd` 曲线，单位 cents |
| VOCALOID4 | `.vsqx` | **tick**（480 tick/四分音符） | 每个音符 `<p>` 标签可留空交给引擎自带 G2P，也可以 `<p lock="1">` **手工锁定音素**，强制引擎按指定音素发声 | `<cc>` 控制器轨道里的 `P`（Pitch Bend）+ `S`（Pitch Bend Sensitivity）双通道 |

三者的共同点是：**只要给出正确的音符起止时间、音高、以及（可选的）音素覆盖**，
虚拟歌手就会严格按这些数字唱出来——SVS Lab Tools 的全部工作就是把参考音频里
"人是怎么唱/念的"这件事,尽可能精确地转换成这三类数字。

---

## 3. 全链路总览：四步管线

```
音频 (WAV) + 歌词文本
        │
        ▼
┌─────────────────────────┐
│ ① 强制对齐               │  MFAProcessor / alt_aligners.py
│   拿到"什么时候发什么音"   │  → .lab（100ns 时间戳 + 音素/音节/单词标签）
└─────────────────────────┘
        │
        ▼
┌─────────────────────────┐
│ ② 音素翻译               │  phoneme_converter.py
│   翻译成目标引擎的音素符号 │  → ARPABET / VOCALOID4 记号 / 假名去母音化符号 ...
└─────────────────────────┘
        │
        ▼
┌─────────────────────────┐
│ ③ 音高提取               │  tsubaki_processor.py + f0_extractors.py
│   给每个时刻一个 F0 频率  │  → DIO/Harvest/CREPE/RMVPE 提取 + 后处理平滑
└─────────────────────────┘
        │
        ▼
┌─────────────────────────┐
│ ④ 工程文件序列化          │  tsubaki_processor.py
│  组装成目标引擎的文件结构  │  → .svp / .ustx / .vsqx
└─────────────────────────┘
        │
        ▼
用户在 Synthesizer V / OpenUTAU / VOCALOID4 里打开，点击播放，
虚拟歌手"唱"出与参考音频节奏、音高高度贴合的声音
```

`pipeline.py` 里的 `AudioProcessingPipeline._run_alignment()` 是①的统一入口，
`aligner_backend` 参数决定走 MFA 还是替代对齐后端；②③④集中在 `TsubakiProcessor`
类（`tsubaki_processor.py`）内部顺序执行。下面逐步拆开看。

---

## 4. 第一步：强制对齐 —— 拿到"什么时候发什么音"

### 4.1 什么是强制对齐

强制对齐（Forced Alignment）指的是：**已知一段音频对应的完整文本内容，反推文本里
每个字/音素分别落在音频的哪个时间区间**。这与"语音识别"（不知道说了什么，靠模型
猜）正好相反——强制对齐是"知道说了什么，只求时间戳"，因此可以做到比自由识别高得多
的时间精度，这正是驱动虚拟歌手所必需的。

### 4.2 默认后端：MFA（Montreal Forced Aligner）

`mfa_processor.py` 里的 `MFAProcessor` 类调用 MFA 命令行工具（基于 Kaldi 声学模型），
输出 TextGrid（词层 + 音素层两条轨道）。随后针对语言分别做"词 → 音素分配"的后处理：

- `_process_zh_words`：中文按拼音音节切分，把词层对应的时间区间按音节数等分／按
  MFA 音素层精修，产出 `拼音音节` 级别的 LAB 条目；
- `_process_en_words`：英语优先用 MFA 音素层给出的真实 ARPABET 时间戳，音素层为空
  时用 `word_to_arpabet_g2p_only()` 做 G2P 兜底，再按 `distribute_arpabet_phones()`
  的音素时长权重表（元音最长、塞音最短）把整词的时间跨度按比例切给各个音素；
- `_process_ja_words` / `_process_ko_words` / `_process_yue_words`：日语按假名音拍、
  韩语按谚文（Hangul）音节、粤语按 Jyutping 分别做类似的时间切分。

### 4.3 四种可替换的对齐后端（`alt_aligners.py`）

当用户不想装 MFA、或想要更高精度/更快速度时，可以切换 `aligner_backend`：

| 后端 | 本质 | 适用场景 |
|---|---|---|
| WhisperX | 词级时间戳 ASR + 强制对齐 | 无需预装 MFA 词典，支持更多语言组合 |
| Qwen3-ASR | 通义 Qwen3 语音识别模型（经 `:5001` 微服务调用） | 中文/多语种识别精度较高 |
| Qwen3-ForcedAligner | 通义 Qwen3 专用强制对齐模型 | 长音频、中英混排场景 |
| NeMo-FA | NVIDIA NeMo CTC 强制对齐（经 `:5002` 微服务调用） | 对特定语言的音素级精度较好 |

这四种后端产出的原始时间戳质量参差不齐，`alt_aligners.py`（全仓最大文件，5,258
行）里超过一半的代码是**后处理纠错算法**，而不是简单的"调用 API 拿结果"，包括：

- `_inject_sentence_pauses` / `_refine_sil_boundaries_by_energy`：用音频 RMS 能量
  曲线精修静音边界，避免相邻句子音素粘连；
- `_plan_sentence_aligned_chunks` / `_plan_chunks_via_whisperx_rough_pass`：长音频
  分块规划，让切分点尽量落在句子边界（不切在发音中间），并用 WhisperX 粗对齐先行
  修正纯字符比例估算在中英混排时的严重偏差；
- `_explode_to_single_char_entries` / `_distribute_mora_across_chars`：把词级/音节级
  时间戳进一步拆分到字符/音拍级，这是虚拟歌手能"逐字对上口型"的关键一步——如果
  停留在整句/整词级时间戳，工程文件里就只能生成寥寥几个超长音符，唱不出歌词该有的
  节奏感。

### 4.4 这一步的产物：`.lab` 文件

不管走哪条对齐路径，最终都落地为统一格式：每一行是
`起始时间(100ns) 结束时间(100ns) 标签`，标签可以是拼音音节、ARPABET 音素、假名、
韩语音节等，取决于语言。这份 `.lab` 就是后续所有步骤的共同输入——**它本质上是
"虚拟歌手要唱的每一个音，精确到 100 纳秒的节奏谱"**。

---

## 5. 第二步：音素翻译 —— 把 MFA/ASR 的输出改写成引擎认识的符号

`.lab` 里的标签（拼音、IPA、假名等）不是任何一款合成引擎能直接识别的音素记号，
必须先"翻译"一遍。这一步的核心逻辑在 `phoneme_converter.py`（1,832 行），它维护
了一整套面向不同引擎、不同语言的转换表。这是三款引擎里"发音是否准确"差异最大的
环节，值得展开讲。

### 5.1 英语：IPA → ARPABET → VOCALOID4 专属记号

`convert_phoneme()` 先把 MFA 输出的 IPA 音素（如 `iː`、`ɪ`、`ʌ`）通过
`EN_IPA_TO_ARPABET` 映射表转成 ARPABET（`iy`、`ih`、`ah`），Synthesizer V 的
`phonemes` 字段直接使用这套 ARPABET。

VOCALOID4 使用的是自己的一套音素符号体系，不能直接吃 ARPABET，因此
`arpabet_to_vocaloid4()` 做了进一步转换，核心规则：

- 去掉重音数字、统一小写；
- 元音 + 音节尾 `r` 合并成儿化元音（判定条件是 `r` 后面不再紧跟元音，否则视为
  连读声母不合并）；
- 词首的塞音 `b/d/g/p/t/k/l` 使用送气/强化的"词首形式"（`_ARPABET_TO_V4_WORD_INITIAL`
  单独查表），其余位置用基础表 `_ARPABET_TO_V4_BASE`；
- 例如 `["hh","ah","l","ow"]`（hello 的 ARPABET）被转换为
  `"h V l @U"`——这正是 VOCALOID4 工程文件里 "hello" 这个词的标准音素写法。

英语单词整体的时间跨度还要按音素类型比例细分到每个音素：`distribute_arpabet_phones()`
维护了一张音素时长权重表——双元音/长元音权重最高（1.2~1.5）、鼻音/流音居中
（0.9~0.95）、塞音最短（0.5~0.6，因为塞音天然由"闭塞+爆破"构成，发音短促）,
按权重归一化后切分整词的时间区间，这样切出来的每个音素时长才符合真实发音的
长短比例，而不是简单地把时间平均分成 N 份。

### 5.2 日语：假名合并 + 去母音化（母音無声化）专属符号

日语是三种语言里发音规则最细的一支，因为它涉及 VOCALOID 社区最看重的"母音無声化"
（清音化）现象——比如"です"的"す"在自然口语中常常不发出元音 u，只留下清辅音 s
这个气声。`build_ja_hiragana_lab()` / `build_ja_merged_lab()` 负责把逐音素的 romaji
（辅音+元音两段）合并回完整假名音节，并且在原始 LAB 中该音节的元音被标记为大写
`I`/`U`（代表"这次演唱里确实被去母音化唱出来了"）时，通过
`ja_devoiced_onset_to_vocaloid4()` 计算出对应的**引擎专属去母音化符号**：

- 多数辅音去母音化后符号不变（す → 仍是 `s`，つ → 仍是 `ts`）；
- 但き/ひ/ぴ 有专属记号：き → `k'`，ひ → `C`，ぴ → `p'`；
- ふ 固定使用 `p\`（与是否去母音化无关）；
- 这套映射依据的是 VOCALOID4 Editor 官方手册附录音素表，直接写入 `<p lock="1">`
  才能让 VOCALOID 引擎正确唱出去母音化的气声效果，普通假名歌词交给引擎自带 G2P
  是无法触发这个效果的。

SynthesizerV 走的是另一套更简单的规则（`target="synthesizerv"`
分支）：去母音化只是删掉元音、辅音记号原样保留，没有 VOCALOID4 那样的专属符号，
但对用户指定的 き/ひ/ぴ、く/ふ/ぷ 会保留元音写法（如 `"k i"`）。两套规则由同一个
函数按 `target` 参数分流，避免逻辑分叉导致两个格式的去母音化效果不一致。

### 5.3 中文 / 粤语 / 韩语

- 中文：拼音音节直接作为标签，不做额外音素级拆分（多数 SVS 声库本身按拼音音节
  驱动发音）；
- 粤语：Jyutping（粤拼）音节，同理直接透传；
- 韩语：谚文（Hangul）拆解为初声/中声/终声三段 Jamo，分别对应时间区间。

### 5.4 用户自定义词典优先级

`dictionary_manager.py` 支持用户创建任意数量、任意命名的"单词→音素"词典（区分
`synthesizerv`/`vocaloid` 两种记号体系）。在写入工程文件音素字段时，优先级是：

```
去母音化音素（这一次录音的客观声学事实） 
    > 用户自定义词典命中 
        > 英语 G2P 兜底（word_to_arpabet） 
            > 默认（留空，交给引擎自带发音引擎）
```

这个优先级顺序本身也是一处工程细节：去母音化被排在最前，是因为它代表"这一次
演唱确实这样唱了"这个具体事实，而词典是与具体演唱无关的静态替换规则——两者冲突时
应该保留更具体的声学事实，而不是让通用规则覆盖掉它。

---

## 6. 第三步：音高提取 —— 给每个音符一个"该唱多高"

### 6.1 四种 F0（基频）提取方法

`AudioProcessingConfig.f0_method` 可选四种（`tsubaki_processor.py` +
`f0_extractors.py`）：

| 方法 | 实现 | 特点 |
|---|---|---|
| DIO | PyWORLD，内置，走隔离子进程 | 速度快 |
| Harvest | PyWORLD，内置，走隔离子进程 | 精度更高但更慢 |
| CREPE | `f0_extractors.py::extract_f0_crepe`，基于深度学习（torchcrepe） | 支持 `full`/`tiny` 两档模型，CPU/CUDA 自适应 |
| RMVPE | `f0_extractors.py::RMVPEF0Extractor`，基于深度学习 | 模型架构随包附带（vendored），需下载权重文件 |

其中 DIO/Harvest 走的 `_run_pyworld_isolated()` 有一处专门的健壮性设计：PyWORLD 是
编译好的 C++ 原生扩展，在部分环境下可能触发操作系统级崩溃（Access Violation），
这类错误 Python 的 `try/except` 完全无法捕获，会直接拖垮整个 Flask 主进程。因此
DIO/Harvest 调用被放进 `multiprocessing.get_context("spawn")` 起的独立子进程里，
主进程只通过 `Queue` 收结果并设超时，即使子进程被系统杀死，也只表现为"队列超时"，
不会拖累虚拟歌手唱到一半整个服务突然消失。

### 6.2 F0 后处理：`_post_process_f0`

原始 F0 曲线不能直接喂给工程文件，要先经过一套后处理管线（按顺序）：

1. 清除非有限值、越界值（`< f0_floor*0.6` 或 `> f0_ceil*1.15`）置零；
2. 对 ≤3 帧的短促静音间隙做 `log2` 域线性插值桥接，避免曲线过度碎片化；
3. `_soft_reject_spikes`：对接近 `f0_ceil*0.92` 的可疑高频点做软性尖峰剔除（限制
   相邻跳变不超过 3 个半音）；
4. 对每一段连续有声区间：先做窗口=3 的中值滤波去毛刺，再在 **log2 频率域**（而非
   线性 Hz 域）做移动平均平滑——半音是对数关系，log2 域平滑才不会出现"高音区过
   平滑、低音区欠平滑"的失真；
5. `_correct_octave_errors`：纠正八度跳变（DIO/Harvest 这类传统算法常见的倍频/
   半频误判，比如把一个音突然识别成高一个八度或低一个八度）；
6. 最终裁剪回 `[f0_floor, f0_ceil]` 范围。

这套流程决定了工程文件里每个音符最终落在哪个半音、以及音高曲线是否平滑自然——
如果跳过这一步直接用原始 F0，虚拟歌手唱出来的旋律线会有明显的毛刺、抖动甚至
跳八度的错误。

### 6.3 从 F0 曲线到"音符音高"

有了平滑后的 F0 曲线，每个音符的音高（MIDI note number）取该音符对应时间区间内
所有有声帧 F0 值，先转换到 **MIDI 半音空间**（`69 + 12*log2(f0/440)`）取**中位数**
再四舍五入——用半音空间而不是 Hz 空间取中位数/平均，是因为 Hz 是非线性刻度，直接
在 Hz 域平均会系统性偏向高频段。这个逻辑在 SVP/USTX/VSQX 三种格式的音符生成代码里
是完全一致的实现（`_build_svp_track_payload` / `_build_ustx_track_payload` /
`_build_vsqx_part_xml` 各自独立实现了同一套换算，保证三种格式的音高判定结果一致）。

音符音高确定之后，F0 曲线与该"整数音高"之间的**残差**（连续的、带微小抖动的部分）
才是真正驱动虚拟歌手"唱得有起伏感"的音高曲线数据，会分别写入 SVP 的
`pitchDelta`、USTX 的 `pitd` curve、VSQX 的 Pitch Bend 控制器——这一点在下一节
展开。

---

## 7. 第四步：工程文件序列化 —— 三种格式各自的"发声契约"

这是把前三步的产物（音素时间轴 + 目标记号 + 音高数据）真正组装成引擎能打开的
文件格式的最后一步，三种格式在时间单位、音符结构、音高曲线载体上各不相同，是
理解"为什么同一份 LAB 能喂给三家不同引擎"的关键。

### 7.1 Synthesizer V（`.svp`）

- **时间单位**：blick，一种与 BPM 绑定的整数刻度，换算系数是
  `offset_ratio = bpm/60 * 705,600,000`；LAB 的 100ns 整数时间戳经
  `onset = start_100ns * offset_ratio / 10,000,000` 换算成 SVP 用的 blick 整数
  位置（这与"1 blick = 100ns 的绝对时间"不同，SVP 的 blick 轴会随 BPM 缩放）；
- **音符结构**：`library[0].notes` 数组，每个音符含 `onset`（blick 位置）、
  `duration`（blick 长度）、`pitch`（MIDI note）、`lyric`（歌词/标签）；可选
  `phonemes` 字段覆盖引擎自带 G2P；
- **音高曲线**：`library[0].parameters.pitchDelta`，`mode: "cubic"`，`points` 是
  `[位置, 偏差cents, 位置, 偏差cents, ...]` 的扁平列表，偏差值 = `(F0对应MIDI音高 -
  该时刻所属音符的整数音高) * 100`（cents）；
- **静音处理**：LAB 里真正的静音段（sil/pau/sp）不生成音符，直接在时间轴上留出
  物理空白；辅音起始占位符 `-` 会被保留为实际音符（不能跳过，否则辅音发不出来）。

### 7.2 OpenUTAU（`.ustx`）

- **时间单位**：tick，480 tick = 一个四分音符，`ticks_per_sec = bpm/60 * 480`；
- **音符结构**：`notes` 列表，`position`/`duration`（tick）、`tone`（MIDI note）、
  `lyric`；日语长音符 `ー` 和辅音占位符 `-` 都被规范化成 OpenUTAU 自己的延音记号
  `+`（"consonant tie"），这是 USTX 格式专属的写法，与 SVP/VSQX 的语义不同；
- **音高曲线**：`voice_part["curves"]` 列表里 `abbr: "pitd"` 的曲线，单位 cents
  （100 = 1 个半音）。这里代码里保留了两条修复记录，值得一提：F0 数据最初被误写进
  每个 `note["pitch"]["data"]` 字段（该字段实际是 OpenUtau 用于**音符间过渡**的
  内部字段，不是全局音高偏移曲线），导致 PITD 曲线始终为空；同时单位换算最初用了
  `×1000` 而非正确的 `×100`，导致所有偏差被放大 10 倍。这两处修复后，PITD 曲线才
  能正确驱动虚拟歌手的音高微调。

### 7.3 VOCALOID4（`.vsqx`）

- **时间单位**：tick，同样 480 tick/四分音符；
- **音符结构**：`<note>` 元素，`<p>` 标签控制发音——留空 `<p></p>` 交给 VOCALOID
  自带 G2P，写入 `<p lock="1"><![CDATA[...]]></p>` 则**强制锁定**手工指定的音素
  （不锁定的话，用户在编辑器里稍微碰一下歌词，VOCALOID 就会按自己的词典重新生成
  发音，之前精心计算的去母音化等效果会被覆盖掉）；
- **音高曲线**：走 `<cc>` 控制器轨道，两个通道配合使用——`S`（Pitch Bend
  Sensitivity，本项目固定写入 13 个半音）定义了 `P`（Pitch Bend）通道 `±8190`
  的整数范围对应多少个半音，音高偏差换算为
  `p_val = round(偏差半音 / 13 * 8190)`；曲线在写入前会先按固定步长重采样成密集
  网格，并且**必须在每个音符起点精确写一个新值**，否则上一个音符的音高会在新
  音符开始后延续若干毫秒，造成明显的起音跳变。
- **PIT 曲线平滑的一处工程细节**：早期版本对 PIT 曲线做平滑/降采样时没有按音符
  边界切分，导致相邻音符交界处出现锯齿状毛刺音高，后来的修复把平滑操作限制在
  **单个音符内部**进行（`vsqx_pitch_smooth_window` 配置项 + 中值预滤波），不再
  跨音符边界平滑，交界处的毛刺才消失。

### 7.4 三种格式的共同基础设施

尽管时间单位、音符字段、音高曲线载体各不相同，三者共享同一套上游数据（LAB 时间轴
+ 音素记号 + F0 曲线），并且"整数音符音高怎么从 F0 算出来"这套逻辑在三处独立实现
但结果一致（见 §6.3），这保证了同一次处理导出的 SVP/USTX/VSQX 三个文件，虚拟歌手
唱出来的**旋律线和节奏感是相同的**，只是各自遵循目标软件的文件格式约定。

---

## 8. 完整案例：一句日语歌词如何变成 VOCALOID4 能唱的样子

用"すき"（喜欢，常见口语中"す"会去母音化）走一遍全流程，帮助把前面几节串起来：

1. **强制对齐**：MFA 对齐一段唱"すき"的参考音频，MFA 日语声学模型输出音素层
   IPA/罗马字，`_process_ja_words` 切出两个音节区间：`s+u` 对应 0.00s–0.15s，
   `k+i` 对应 0.15s–0.40s；
2. **音素翻译**：`build_ja_merged_lab()` 把 `s+u` 合并回假名音节"す"，同时发现原始
   LAB 里这个音节的元音被标注为去母音化（大写 `U`），于是调用
   `ja_devoiced_onset_to_vocaloid4("s", "u", target="vocaloid4")` —— `s` 不在专属
   记号表里，原样返回 `s`（多数辅音去母音化后符号不变）；"き"没有被标记去母音化，
   按正常流程转换；
3. **写入音素字段**：VSQX 分支给"す"这个音符写入 `<p lock="1"><![CDATA[s]]></p>`
   ——只有清辅音 `s`，没有元音 `u`，锁定后 VOCALOID4 会按这个指定音素发出接近气声
   的效果，而不是完整的"su"；"き"按正常假名走引擎自带 G2P 或对应罗马字音素；
4. **音高**：这两个音节各自的时间区间内提取到的 F0 中位数分别转换成两个整数音高
   （MIDI note），F0 曲线相对各自音符音高的偏差被换算成 `<cc>` 里的 `P` 控制器序列；
5. **产物**：打开生成的 `.vsqx`，能看到两个音符，"す"被锁定为清辅音发音，音高曲线
   带有从参考音频里提取出的自然起伏——虚拟歌手唱出来的效果，会明显区别于用户自己
   随手打"すき"两个字、交给 VOCALOID 默认 G2P 唱出来的版本（默认 G2P 不知道这一次
   演唱里"す"被去母音化了，会老老实实唱出完整的"su"）。

这个例子说明了 SVS Lab Tools 最核心的价值：**它把参考音频里那些"听起来很自然、但
如果让虚拟歌手自己按文字瞎猜发音就绝对猜不出来"的细节（去母音化、连读、真实的
音高起伏），转换成引擎能理解并严格执行的显式指令**。

---

## 9. 多轨/对话批处理：多段音频拼成一个工程

`DialogueBatch.vue`（前端）+ `pipeline.process_dialogue_batch()`（后端）支持一次
处理多个独立的"对话框"（每个框可以是一段音频，也可以是先用 TTS 合成语音再对齐），
最终合并进**同一个多轨工程文件**：

- `build_svp_project_text_multitrack` / `build_utau_project_text_multitrack` /
  `build_vsqx_project_text_multitrack`：每个对话框独立占一条音轨（并行布局），适合
  多角色对话场景；
- `_build_svp_project_text_sequenced` / `_build_ustx_project_text_sequenced` /
  `_build_vsqx_project_text_sequenced`：多段依次拼接在同一条轨道上（顺序布局），
  适合长音频被切分成多段处理的场景，VSQX 分支里 `_build_vsqx_part_xml` 支持通过
  `t_start` 参数把每个 `<vsPart>` 定位到时间轴上正确的累计偏移量。

两种拼装策略共享上文①～③的全部逻辑（对齐 → 音素翻译 → F0），只是在④组装工程文件
时按并行或顺序两种方式排布音轨/片段，避免"多轨场景另写一套生成逻辑"导致行为分叉。

---

## 10. MIDI 输入路径：跳过 F0 判定，直接借音符音高

当用户已经有一份 MIDI 文件（比如从编曲软件导出的旋律），可以让虚拟歌手直接唱
MIDI 里写好的音高，而不是靠 F0 提取去"猜"参考音频的音高。`midi_processor.py`
的 `parse_midi_notes()` 解析出 `(start_sec, end_sec, pitch)` 列表，
`map_segment_to_midi_pitch()` 找出每个 LAB 段落时间范围内重叠的 MIDI 音符音高。

在 SVP/USTX/VSQX 三处音符生成代码里，音高判定优先级统一是：

```
该段有 MIDI 音符重叠？ → 直接用 MIDI 音高（最高优先级，忽略 F0）
        │ 否
        ▼
config.refine_pitch=True？ → 用该段 F0 中位数半音
        │ 否
        ▼
默认使用 config.base_pitch（固定基准音高，适合纯 F0 曲线导出场景）
```

这意味着"仅生成工程"模式下，如果用户提供了 WAV+LAB+MIDI 三件套，最终虚拟歌手唱出
的**节奏**（音符起止时间）来自 LAB，**旋律**（每个音符具体唱多高）来自 MIDI，
两者独立控制、互不干扰。

---

## 11. 这套系统的边界：它做的和不做的

明确边界有助于理解"为什么某些效果无法通过 SVS Lab Tools 实现"：

**它做的**：
- 把参考音频的节奏、发音细节、音高走势，转换成三种格式各自能理解的显式指令
  （音符时间、锁定音素、音高曲线）；
- 在音素/音高层面做了大量针对"听感自然度"的后处理（去母音化、log2 域平滑、
  八度纠错、音素时长按类型加权分配等）。

**它不做的**：
- 不训练、不携带、不修改任何声库（音色完全由用户在 Synthesizer V/OpenUTAU/
  VOCALOID4 里选择的声库决定，SVS Lab Tools 生成的工程文件对声库选择没有任何
  约束或建议）；
- 不做音频渲染/合成，产物永远是结构化的工程文件，不是 wav；
- 不能凭空"发明"参考音频里不存在的音高或节奏——所有输出都是对参考音频/MIDI/
  文本的忠实转换，音准和节奏的上限取决于参考素材本身的质量以及第一步强制对齐的
  准确度。

---

## 12. 关键代码位置速查表

| 环节 | 文件 | 关键函数/类 |
|---|---|---|
| 对齐入口路由 | `backend/pipeline.py` | `_run_alignment()`、`AudioProcessingPipeline.process_full()` |
| MFA 对齐 + 分语言后处理 | `backend/mfa_processor.py` | `MFAProcessor`、`_process_zh_words`/`_process_en_words`/`_process_ja_words`/`_process_ko_words`/`_process_yue_words` |
| 替代对齐后端 + 后处理算法群 | `backend/alt_aligners.py` | `WhisperXAligner`/`Qwen3ASRAligner`/`Qwen3ForcedAligner`/`NeMoForcedAligner`、`get_aligner()` |
| 英语音素转换 | `backend/phoneme_converter.py` | `EN_IPA_TO_ARPABET`、`word_to_arpabet()`、`arpabet_to_vocaloid4()`、`distribute_arpabet_phones()` |
| 日语假名合并 + 去母音化 | `backend/phoneme_converter.py` | `build_ja_hiragana_lab()`、`build_ja_merged_lab()`、`ja_devoiced_onset_to_vocaloid4()`、`apply_phoneme_mode()` |
| 用户自定义词典 | `backend/dictionary_manager.py` | `lookup_word()`、`get_notation()` |
| F0 提取（DIO/Harvest） | `backend/tsubaki_processor.py` | `_run_pyworld_isolated()`、`process_audio_f0()` |
| F0 提取（CREPE/RMVPE） | `backend/f0_extractors.py` | `extract_f0_crepe()`、`RMVPEF0Extractor` |
| F0 后处理 | `backend/tsubaki_processor.py` | `_post_process_f0()`、`_soft_reject_spikes()`、`_correct_octave_errors()` |
| SVP 生成 | `backend/tsubaki_processor.py` | `_build_svp_track_payload()`、`_build_svp_project_text()`、`build_svp_project_text_multitrack()` |
| USTX 生成 | `backend/tsubaki_processor.py` | `_build_ustx_track_payload()`、`_build_utau_project_text()`、`build_utau_project_text_multitrack()` |
| VSQX 生成 | `backend/tsubaki_processor.py` | `_build_vsqx_part_xml()`、`_build_vsqx_project_text()`、`build_vsqx_project_text_multitrack()` |
| MIDI 音高映射 | `backend/midi_processor.py` | `parse_midi_notes()`、`map_segment_to_midi_pitch()` |
| 对话批处理编排 | `backend/pipeline.py` | `process_dialogue_batch()` |

---

*本报告基于当前仓库源码静态阅读整理，未运行完整流程验证音频听感效果，函数行为
以实际源码为准；如后续代码有调整，本报告中标注的具体系数（如 705,600,000、PBS=13
等）及函数名可能需要同步更新。*
