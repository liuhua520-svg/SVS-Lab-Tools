# SVS Lab Tools — 技术架构与代码剖析报告

> 本报告基于对仓库全量源码（后端 Python ≈ 29,000 行 / 前端 Vue+TS ≈ 22,100 行，共约 51,000 行）
> 的静态阅读整理而成，目的是给后续维护者、贡献者或审阅者提供一份可以按图索骥的架构地图，
> 而不是重复 `README.md` 已有的功能/使用说明。
>
> 关于「本工具做什么」「怎么安装/使用」，请看根目录 `README.md`；本报告只回答
> **「代码是怎么组织的」「每个模块负责什么」「关键算法怎么设计的」「有什么工程上的
> 取舍与坑」**。

---

## 目录

1. [项目定位](#1-项目定位)
2. [系统架构总览](#2-系统架构总览)
3. [目录结构详解](#3-目录结构详解)
4. [后端模块地图](#4-后端模块地图)
5. [前端模块地图](#5-前端模块地图)
6. [核心数据流：五种处理模式](#6-核心数据流五种处理模式)
7. [对齐后端适配器架构（alt_aligners.py 深度剖析）](#7-对齐后端适配器架构alt_alignerspy-深度剖析)
8. [音高处理与工程文件生成引擎（tsubaki_processor.py 深度剖析）](#8-音高处理与工程文件生成引擎tsubaki_processorpy-深度剖析)
9. [多语言音素转换体系](#9-多语言音素转换体系)
10. [文本预处理引擎（text_processor.py）](#10-文本预处理引擎text_processorpy)
11. [字幕子系统](#11-字幕子系统)
12. [TTS 子系统](#12-tts-子系统)
13. [MIDI 处理](#13-midi-处理)
14. [设置系统（app_settings.py）](#14-设置系统app_settingspy)
15. [API 路由总表](#15-api-路由总表)
16. [命令行入口（commandline.py）](#16-命令行入口commandlinepy)
17. [打包与分发](#17-打包与分发)
18. [依赖矩阵与环境隔离设计](#18-依赖矩阵与环境隔离设计)
19. [工程亮点](#19-工程亮点)
20. [已知技术债与可改进点](#20-已知技术债与可改进点)

---

## 1. 项目定位

SVS Lab Tools（内部代号 **Tsubaki**）不是一个歌声合成引擎，而是一座「标注 + 音高提取 +
工程文件生成」的桥梁工具：把「一段音频 + 对应文本」转换成可以直接在 Synthesizer V /
VOCALOID4 / OpenUtau 里打开、继续编辑的工程文件。它的核心价值链是：

```
音频 + 文本  ──强制对齐──▶  .lab（音素级时间戳）
                                   │
                                   ├──F0 提取──▶ 音高曲线
                                   │
                                   ▼
                     工程文件生成（SVP / USTX / VSQX）
```

项目主要面向中文用户（文档、界面默认简体中文），同时对 zh / zh-TW / en / ja / ko /
yue（粤语，仅标注环节）提供不同程度的多语言支持。

---

## 2. 系统架构总览

### 2.1 四进程微服务架构

这不是一个单体 Flask 应用，而是 **4 个独立 Python 进程**，彼此通过本地 HTTP 通信：

| 进程 | 脚本 | 端口 | 独立环境目录 | 职责 |
|---|---|---|---|---|
| 主服务 | `backend/app.py` | `127.0.0.1:5000` | `mfa_env`（`.mfa_env`） | HTTP API 网关、MFA/PyWORLD/CREPE/RMVPE、工程文件生成、静态前端托管 |
| Qwen3 服务 | `backend/qwen3_server.py` | `127.0.0.1:5001` | `qwen3_env` | Qwen3-ASR-1.7B / Qwen3-ForcedAligner-0.6B 推理 |
| NeMo 服务 | `backend/nemo_server.py` | `127.0.0.1:5002` | `nemo_env` | NVIDIA NeMo Forced Aligner（CTC 强制对齐）推理 |
| Qwen3-TTS 服务 | `backend/qwen3tts_server.py` | `127.0.0.1:5003` | `qwen3tts_env` | Qwen3-TTS 三种模式（CustomVoice / VoiceDesign / VoiceClone）语音合成 |

主服务 `app.py` 通过 `requests` 以 HTTP 方式调用另外三个服务（常量定义在
`app.py:1043-1045`：`_QWEN3_BASE_URL` / `_NEMO_BASE_URL` / `_QWEN3TTS_BASE_URL`），
前端始终只与 `:5000` 交互，完全不感知后面还有另外三个进程。

**为什么要拆成 4 个进程 + 4 套独立虚拟环境**，而不是一个 `requirements.txt` 装到底？
仓库里每个 `requirements-*.txt` 文件头部都写明了理由（详见 [§18](#18-依赖矩阵与环境隔离设计)）：
NeMo / Qwen3-ASR / Qwen3-TTS 各自对 `transformers`、`packaging`、`torch` 版本有互斥的
硬性要求，混装会导致 `pip` 依赖求解互相"打架"、把对方需要的版本悄悄降级。用 HTTP
微服务边界换取"依赖零冲突"，是这个项目最核心的架构决策。

### 2.2 进程编排：launcher.py

`launcher.py`（550 行）是打包发布形态下的总控进程，职责很"薄"（刻意不 import
torch/nemo 等重依赖，PyInstaller 打包体积小）：

- 读取 `backend/settings/app_settings.json` 里的 `skip_start_qwen3_server` /
  `skip_start_nemo_server` / `skip_start_qwen3tts_server` 开关，决定本次是否要拉起
  对应子进程（这些开关只影响下次完整启动，不影响正在运行的进程）；
- 用 `subprocess.Popen` 以各自 `runtime/<env>/python.exe` 解释器分别拉起
  `app.py`（`mfa_env`）、`qwen3_server.py`（`qwen3_env`）、`nemo_server.py`（`nemo_env`）、
  `qwen3tts_server.py`（`qwen3tts_env`）**四个**服务（`SERVICES` 列表，
  `launcher.py:162-167`）；
- 轮询 `/api/health` 直到主服务就绪（`_wait_for_backend_ready(timeout=30.0)`，最多
  30s 超时，超时也会继续开窗口，前端自行重试）；
- 用 `pywebview` 起一个原生窗口加载 `http://127.0.0.1:5000`，`pystray` 提供系统托盘
  图标（"打开界面" / "退出所有服务"）；
- 退出时先 `terminate()` 已跟踪的子进程 PID，再用 `psutil` 按命令行关键字扫描一遍、
  兜底清理"设置页点过重启"产生的孤儿进程（因为 `/restart` 路由是
  "关端口 → 开新进程 → 旧进程 `os._exit(0)`"，PID 会变）。

> **历史修复记录**：`launcher.py` 的 `SERVICES` 列表早期只登记了 `app`/`qwen3`/`nemo`
> 三项，遗漏了 `qwen3tts_server.py`（打包后的"一键启动"曾经不会自动拉起 5003 端口的
> Qwen3-TTS 服务，需要手动另开进程）。经核对最新源码，这个遗漏**现已修复**：`SERVICES`
> 补上了第四项 `{"name": "qwen3tts", "script": "qwen3tts_server.py", "env":
> "qwen3tts_env", "skip_key": "skip_start_qwen3tts_server"}`，与另外两个可选微服务
> 的注册方式完全对齐，`skip_start_qwen3tts_server` 这个此前"存在但没被读取"的设置
> 字段现在也接上了实际的启动/跳过逻辑。源码注释里同时说明：即便
> `app_settings.py` 的 `DEFAULT_SETTINGS` 暂未补上该键，`dict.get(skip_key,
> False)` 也会按"不跳过"处理，不影响默认行为。

launcher.py 同时是 **CLI 一次性调用模式**的转发入口：检测到 `argv[1] == "cmd"`
（`_is_cmd_invocation()`）时，完全跳过托盘/原生窗口/四个服务的常规启动流程，只把整条
命令行转发给 `runtime/mfa_env/python.exe backend/app.py cmd ...` 子进程并透传其
stdout/stderr/退出码——这条路径与 [§16](#16-命令行入口commandlinepy) 描述的
`commandline.py` 是同一套机制的两端。

### 2.3 前后端关系

前端是纯静态 SPA（Vue 3 + Vite 构建产物在 `frontend/dist/`），由 `app.py` 的
`serve_frontend()`（`app.py:552-573`）直接托管：`/` 和所有非 `/api/*` 路径都返回
`frontend/dist/index.html`，交给 Vue Router 做客户端路由。开发模式下前端另跑
Vite dev server，通过 axios 直接打 `:5000` 的 API（跨域由 `flask-cors` 处理）。

---

## 3. 目录结构详解

```
SVS-Lab-Tools-main/
├─ backend/                     # 4 个 Flask 进程 + 所有处理逻辑，纯 Python，无 ORM/数据库
│  ├─ app.py                    # 主服务：~65 个 /api/* 路由，作业(job)状态机，静态资源托管
│  ├─ pipeline.py               # AudioProcessingPipeline：串联 MFA/alt-aligner + F0 + 工程导出
│  ├─ mfa_processor.py          # MFAProcessor：调用 MFA 命令行 + 五语言音素后处理
│  ├─ mfa_utils.py              # MFAChecker：探测本机 MFA/conda 环境是否就绪
│  ├─ alt_aligners.py           # 替代对齐后端（WhisperX/Qwen3-ASR/Qwen3-FA/NeMo-FA）适配器，全仓最大文件
│  ├─ tsubaki_processor.py      # F0 后处理 + SVP/USTX/VSQX 工程文件生成引擎
│  ├─ phoneme_converter.py      # IPA → ARPABET/假名/Jyutping/Hangul 音素转换表
│  ├─ f0_extractors.py          # CREPE / RMVPE F0 提取后端（DIO/Harvest 内置于 tsubaki_processor）
│  ├─ midi_processor.py         # MIDI 导入/导出（含歌词事件解析）
│  ├─ text_processor.py         # 文本预处理："优化文本"弹窗的纯字符串转换逻辑
│  ├─ dictionary_manager.py     # 用户自定义"单词→音素"词典（任意数量、可命名）
│  ├─ tts_processor.py          # TTS 引擎注册表（SAPI5/EdgeTTS/Qwen3-TTS）+ 分句 + 强制对齐
│  ├─ subtitle_processor.py     # 字幕识别方向：音频/视频 → SRT/LRC/TXT/LAB
│  ├─ subtitle_import.py        # 字幕导入方向：SRT/LRC + 音频 → 逐句切片强制对齐
│  ├─ app_settings.py           # 全局设置读写、HF 镜像/离线开关、对齐调优参数
│  ├─ commandline.py            # CLI 入口：不起 HTTP 服务，单次执行即退出
│  ├─ qwen3_server.py           # 独立微服务：Qwen3-ASR / Qwen3-ForcedAligner（:5001）
│  ├─ nemo_server.py            # 独立微服务：NeMo Forced Aligner（:5002）
│  ├─ qwen3tts_server.py        # 独立微服务：Qwen3-TTS 三模式语音合成（:5003）
│  ├─ requirements.txt          # mfa_env 依赖（主服务）
│  ├─ requirements-qwen3.txt    # qwen3_env 依赖
│  ├─ requirements-nemo.txt     # nemo_env 依赖
│  └─ requirements-qwen3tts.txt # qwen3tts_env 依赖
│
├─ frontend/                    # Vue 3 + TypeScript + Element Plus SPA
│  ├─ src/
│  │  ├─ App.vue                # 顶层布局：菜单、路由出口、VOCALOID 风格明/暗主题
│  │  ├─ main.ts                # 应用入口
│  │  ├─ i18n.ts                # 5 语言（zh/zh-TW/en/ja/ko）文案表，5134 行，全仓最大前端文件
│  │  ├─ router/index.ts        # 9 个页面路由，懒加载
│  │  └─ components/
│  │     ├─ MFAProcessor.vue    # 主页：对齐+F0+工程生成的核心表单（5003 行）
│  │     ├─ DialogueBatch.vue   # 对话/多轨批处理页（4594 行）
│  │     ├─ SubtitleRecognizer.vue  # 字幕识别页
│  │     ├─ SubtitleEditor.vue      # 字幕编辑页
│  │     ├─ SubtitleWaveform.vue    # 波形可视化组件
│  │     ├─ DictionaryManager.vue   # 词典管理页
│  │     ├─ EnglishG2P.vue          # 英文 G2P 查询页
│  │     ├─ SettingsPage.vue        # 设置页
│  │     ├─ AboutPage.vue / HelpPage.vue
│  │     └─ useSubtitleHistory.ts   # 字幕编辑撤销/重做 composable
│  └─ dist/                     # `npm run build` 产物，被 app.py 直接托管
│
├─ launcher.py                  # 打包发布形态的进程编排 + 托盘 + 原生窗口
├─ build_launcher.bat           # PyInstaller 打包 launcher.py
├─ pack_runtime.bat             # conda-pack 生成便携版 runtime/{mfa,qwen3,nemo}_env
├─ setup.bat / setup.sh         # 源码运行模式的环境安装脚本
├─ run.bat / run.sh             # 源码运行模式的启动脚本
├─ Docs/
│  ├─ README/                   # 繁中/英/日/韩 四份 README 翻译
│  └─ LICENSES/                 # 第三方许可证全文（BSD/MIT/Apache/LGPL 等）
├─ NEMO_ALIGNER_INTEGRATION.md  # NeMo 接入的专项技术笔记
├─ QWEN3-TTS_集成说明.md         # Qwen3-TTS 接入的专项技术笔记
├─ ACKNOWLEDGMENT.md            # 第三方依赖鸣谢与许可证清单
└─ README.md
```

---

## 4. 后端模块地图

按代码规模排序（`wc -l`），可以看出项目的"重心"在哪：

| 文件 | 行数 | 一句话职责 |
|---|---:|---|
| `alt_aligners.py` | 5,258 | 4 种替代对齐后端的适配器 + 分块/静音/对齐修复算法群 |
| `app.py` | 4,885 | Flask 路由层，~65 个 API 端点，作业状态机 |
| `tsubaki_processor.py` | 4,007 | F0 后处理 + SVP/USTX/VSQX 三种工程文件生成 |
| `phoneme_converter.py` | 1,832 | IPA → 目标音素集转换表 |
| `mfa_processor.py` | 1,754 | MFA 调用 + 五语言音素分配 |
| `tts_processor.py` | 1,424 | TTS 引擎抽象 + 分句 + 对齐 |
| `commandline.py` | 1,384 | CLI 批处理入口 |
| `pipeline.py` | 1,308 | 五种处理模式的编排层 |
| `text_processor.py` | 1,158 | 纯文本转换（数字读法、符号、大小写等） |
| `subtitle_processor.py` | 1,089 | ASR 字幕识别、VAD、字幕导出/压制 |
| `app_settings.py` | 668 | 全局设置持久化 |
| `subtitle_import.py` | 656 | 字幕驱动的逐句强制对齐 |
| `f0_extractors.py` | 658 | CREPE / RMVPE 提取器 |
| `nemo_server.py` | 652 | NeMo 微服务 |
| `qwen3_server.py` | 577 | Qwen3-ASR/FA 微服务 |
| `qwen3tts_server.py` | 565 | Qwen3-TTS 微服务 |
| `dictionary_manager.py` | 477 | 自定义词典 CRUD |
| `mfa_utils.py` | 369 | MFA 环境自检 |
| `midi_processor.py` | 238 | MIDI 解析/生成 |

模块之间没有循环依赖，大致是一条清晰的三层结构：

```
app.py（HTTP 路由 / 作业管理）
   │
   ▼
pipeline.py（编排：选对齐后端 → 走 F0 → 走导出）
   │                         │
   ▼                         ▼
mfa_processor.py /      tsubaki_processor.py
alt_aligners.py         （F0 后处理 + 工程文件序列化）
（对齐产出 .lab）             │
   │                         ▼
   ▼                    f0_extractors.py（CREPE/RMVPE）
phoneme_converter.py
dictionary_manager.py
```

`text_processor.py`、`midi_processor.py`、`subtitle_processor.py`、
`subtitle_import.py`、`tts_processor.py` 是相对独立的旁支功能，刻意与主对齐管线解耦
（`subtitle_processor.py` 文件头注释明确写了"与现有 MFA/对齐管线完全解耦，避免影响
已经跑通的强制对齐流程"），复用其中的对齐能力时是"调用别的模块的函数"而不是"改主流程"。

---

## 5. 前端模块地图

前端是标准 Vue 3 `<script setup>` + Element Plus + Vite 单页应用，**没有引入 Vuex/Pinia
等全局状态管理库**——从路由懒加载结构看，各页面基本自成一体，通过 axios 直接与后端
`/api/*` 交互，页面间共享状态很少（这与"每个页面对应一种相对独立的处理模式"的产品形态
是匹配的）。

| 路由 | 组件 | 规模 | 职责 |
|---|---|---:|---|
| `/` | `MFAProcessor.vue` | 5,003 行 | 核心页面：上传音频/文本 → 选对齐后端/F0 算法/工程格式 → 提交任务 → 轮询进度 → 下载 |
| `/dialogue` | `DialogueBatch.vue` | 4,594 行 | 多轨/多段"对话框"批量处理（每个 box 可独立配置音频或 TTS 输入） |
| `/subtitle` | `SubtitleRecognizer.vue` | 1,413 行 | 上传音频/视频 → ASR 识别字幕 → 编辑/导出 SRT/LRC/TXT |
| `/subtitle-editor` | `SubtitleEditor.vue` | 1,044 行 | 已有字幕的可视化编辑（配合 `SubtitleWaveform.vue` 波形） |
| `/english-g2p` | `EnglishG2P.vue` | 1,116 行 | 英文文本 → ARPABET 音素查询/统计工具 |
| `/settings` | `SettingsPage.vue` | 848 行 | 模型下载、HF 镜像、对齐调优参数、控制台可见性等全局设置 |
| `/dictionary` | `DictionaryManager.vue` | 902 行 | 自定义词典的增删改查、导入导出 |
| `/about`、`/help` | `AboutPage.vue` / `HelpPage.vue` | 342 / 290 行 | 关于、帮助文档 |

`i18n.ts`（5,134 行）是前端体量最大的单文件，承载 zh/zh-TW/en/ja/ko 五语言的全部文案，
用 `vue-i18n` 驱动；`App.vue` 里的主题切换是 VOCALOID 风格的初音未来配色（teal/purple）
明暗双主题，通过 CSS 自定义属性实现。

---

## 6. 核心数据流：五种处理模式

`pipeline.py` 里的 `AudioProcessingPipeline` 类是编排层，对应 `app.py` 暴露的 5 类
处理任务，每类都是"提交任务拿 job_id → 轮询 `/api/pipeline/job/<id>` → 完成后下载"的
异步作业模式（`app.py` 里 `set_job` / `get_job` / `request_job_cancel` 维护一个内存
job 表，支持取消）：

1. **仅标注（mfa-only）** — `POST /api/pipeline/mfa-only`：只跑对齐，产出 `.lab`，最快。
2. **完整处理（full）** — `POST /api/pipeline/full`：对齐 → F0 提取 → 工程文件，一步到位。
3. **仅生成工程（project-only）** — `POST /api/pipeline/project-only`：已有 `WAV + LAB/MIDI`，
   跳过对齐，直接走 F0 + 导出（适合复用已标注好的素材）。
4. **仅 F0（f0-only）** — `POST /api/pipeline/f0-only`：只提取音高，不生成工程文件。
5. **对话批处理（dialogue batch）** — `POST /api/dialogue/process`：多个"box"（每个可以是
   音频输入或 TTS 生成）批量走完整流程，最终合并进同一个多轨工程文件
   （`tsubaki_processor.py` 的 `build_multitrack_project` / `*_sequenced` 系列方法）。

此外还有两条独立支线复用了同一套对齐能力：

- **字幕导入对齐**（`subtitle_import.py` + `run_subtitle_align_job`）：用户已有 SRT/LRC
  字幕文件 + 完整音频，把字幕时间轴当"切分点+参考文本"，逐条切出小段音频分别对齐。
- **TTS 跟读对齐**（`tts_processor.py` 的 `synthesize_and_align`）：先用 TTS 合成语音，
  再对合成结果做强制对齐，得到与 TTS 语音同步的 `.lab`（服务于"讲述人"功能）。

`aligner_backend` 参数贯穿以上大多数入口，可选 `"mfa"` / `"qwen3_asr"` /
`"qwen3_aligner"` / `"nemo_aligner"`，由 `pipeline.py` 的 `_run_alignment()` 统一分流到
`MFAProcessor.process()` 或 `alt_aligners.get_aligner(backend).align()`。

---

## 7. 对齐后端适配器架构（alt_aligners.py 深度剖析）

这是全仓最大、算法密度最高的文件（5,258 行），本质是一个**适配器模式**：

```python
class AltAlignerBase:            # 抽象基类，定义 align() 接口
class WhisperXAligner(AltAlignerBase):      # 词级时间戳 ASR，degenerate 检测/修复
class Qwen3ASRAligner(AltAlignerBase):      # 通过 :5001 调 Qwen3-ASR
class Qwen3ForcedAligner(AltAlignerBase):   # 通过 :5001 调 Qwen3-FA，含长音频分块规划
class NeMoForcedAligner(AltAlignerBase):    # 通过 :5002 调 NeMo CTC 强制对齐

def get_aligner(backend, device="auto", **kwargs) -> AltAlignerBase:  # 工厂函数 + 实例缓存
def clear_aligner_cache(backend=None) -> int
def get_alt_aligner_status() -> Dict
```

### 7.1 为什么需要这么多后处理算法

四种对齐后端产出的原始结果质量参差不齐（尤其是长音频、混合中英文文本），文件里
超过一半的篇幅是**后处理与纠错算法**，而不是简单的"调 API 拿结果"：

| 函数 | 作用 |
|---|---|
| `_inject_sentence_pauses` | 在句子边界之间人为插入静音，防止相邻句子的音素粘连 |
| `_refine_sil_boundaries_by_energy` | 用音频能量（RMS）曲线精修静音段边界，而不是完全信任模型输出的时间戳 |
| `_fix_ctc_stretch` | 修复 CTC 类对齐器常见的"首尾被拉伸"问题 |
| `_compute_rms_curve` / `_find_quiet_run_center` / `_find_quietest_point` | 在音频里寻找"最安静的点"作为长音频切分点，避免切在发音中间 |
| `_plan_sentence_aligned_chunks` | 长音频分块规划：尽量让切分点落在句子边界，同时满足模型的最大输入时长限制 |
| `_plan_chunks_via_whisperx_rough_pass` | 用 WhisperX 先跑一遍粗对齐，为 Qwen3-FA 等按字数比例估算切分点提供更准的先验（避免纯字符数比例估算在中英混排时严重跑偏） |
| `_merge_short_spans` / `_stitch_spans_to_full_coverage` | 把分块对齐的结果重新拼接回一条完整时间轴，同时保证覆盖率 |
| `_explode_to_single_char_entries` / `_distribute_mora_across_chars` | 把词级/音节级时间戳进一步拆分到字符/音拍（mora）级 |
| `_bind_ref_text_by_asr_count` | 当参考文本与 ASR 识别出的实际字数不一致时，按数量重新绑定映射关系 |

### 7.2 关键工程细节

- **CUDA 探测与自动降级**：`_torch_cuda_usable()` / `_is_cuda_oom_or_env_error()` /
  `_safe_device()` 一组函数处理"声称有 CUDA 但实际不可用"或"显存不足"的场景，自动回退
  到 CPU，而不是直接崩溃。
- **模型路径解析优先级**：`resolve_models_dir()` 按
  `环境变量 TSUBAKI_MODELS_DIR` → `<文件所在目录>/models/` 的顺序解析，其下
  `hf_cache/hub/` 存放 HuggingFace 模型缓存、`rmvpe/` 存放 RMVPE 模型，便于打包发布时
  把模型和代码放在一起分发。
- **繁简转换前置**：Qwen3 系对齐前会调用 `convert_traditional_to_simplified()`
  （基于 `opencc-python-reimplemented`，未安装时自动跳过并记录 warning，不影响其它功能）。
- **标点/静音的处理哲学**：Qwen3 对齐输出不包含标点（标点本身不发声），模块选择"扫描
  时间轴间隙、把 ≥50ms 的空白自动补全为 SIL 条目"，而不是要求用户手动处理标点。
- **导入顺序的隐藏坑**：文件顶部有一段很长的注释解释为什么必须在任何
  `speechbrain`/`qwen_asr` import 之前显式 `import librosa.core.audio`——否则
  `speechbrain` 的懒加载占位模块会在 `inspect.getmodule()` 扫描 `sys.modules` 时被
  意外触发导入，报出与真实原因完全无关的错误信息。这是一段"踩坑记录"式注释，说明
  这类深层依赖冲突在项目历史上真实发生过。

### 7.3 已知修复过的历史问题（据以往会话记录）

- Qwen3-FA 在中英混排文本上出现过 token 序列顺序错乱（整块文字位置对调）；
- `_plan_sentence_aligned_chunks` 早期用字符数比例估算切分点误差较大，WhisperX
  degenerate（退化为均匀插值）对齐失败也曾发生，后改为"WhisperX 粗对齐先行"策略；
- `merge_tokens(blank_id)` 参数传错导致 NeMo 100% 落入均匀回退（uniform-fallback）对齐。

---

## 8. 音高处理与工程文件生成引擎（tsubaki_processor.py 深度剖析）

`TsubakiProcessor` 类（约 3,750 行，占该文件 94%）承担两件事：**F0 音高后处理**和
**三种工程文件格式的序列化输出**。

### 8.1 F0 后处理管线（`_post_process_f0`）

处理顺序（`f0_smooth=False` 时整段跳过，直接返回原始曲线）：

1. 清除非有限值、越界值（`< f0_floor*0.6` 或 `> f0_ceil*1.15`）置零；
2. 对 ≤3 帧的短促静音间隙做 log2 域线性插值桥接（避免过度碎片化）；
3. `_soft_reject_spikes`：对接近 `f0_ceil*0.92` 的可疑高频点做软性尖峰剔除
   （限制相邻跳变 ≤3 个半音）；
4. 对每一段连续有声区间：先做窗口=3 的中值滤波去毛刺，再在 **log2 频率域**
   （而非线性 Hz 域）做移动平均平滑——半音是对数关系，log2 域平滑能避免高音区
   过平滑、低音区欠平滑的失真；
5. 最终裁剪回 `[f0_floor, f0_ceil]` 范围。

此外还有 `_correct_octave_errors`（八度跳变纠错，DIO/Harvest 类算法常见的倍频/半频
误判）等辅助方法。

### 8.2 PyWORLD 子进程隔离——原生崩溃防护

这是一处值得单独强调的健壮性设计。`pw.dio()` / `pw.harvest()` /
`pw.stonemask()` 是编译好的 C++ 扩展，在部分 Windows 环境下会触发
**Access Violation (0xC0000005)** 这类操作系统级错误——这类错误发生在 Python 解释器
之外，`try/except` 完全无法捕获，后果是整个 Flask 主进程被系统静默杀死，且不留
Traceback。

`_run_pyworld_isolated()` 的方案：用 `multiprocessing.get_context("spawn")` 把
PyWORLD 调用放进独立子进程（`_f0_worker`），主进程只通过 `Queue` 收结果并设超时；
即使子进程被系统杀死，主进程也只是"队列超时"，可以返回一个可控的、带诊断建议的
错误信息（提示切换 CREPE/RMVPE，或检查 `.mfa_env` 内原生扩展包版本冲突），而不会
被拖着一起崩溃。代码注释里提到这套隔离机制"此前一直是未被调用的死代码"——即
`_f0_worker` 早就写好了，但 `process_audio_f0()` 实际调用路径走的是进程内直接调用，
真正接上隔离调用桥梁是后来才修复的，说明这是一个真实排查过的生产问题。

### 8.3 F0 提取后端总览

| 方法 | 实现位置 | 特点 |
|---|---|---|
| DIO | `tsubaki_processor.py`（内置，经隔离子进程） | 速度快，PyWORLD 算法 |
| Harvest | `tsubaki_processor.py`（内置，经隔离子进程） | 精度更高，PyWORLD 算法，比 DIO 慢 |
| CREPE | `f0_extractors.py::extract_f0_crepe` | 基于深度学习（torchcrepe），支持 `full`/`tiny` 两档模型规格，CPU/CUDA 自适应 |
| RMVPE | `f0_extractors.py::RMVPEF0Extractor` | 基于深度学习，模型架构在文件内"随包附带"（vendored），需要 `rmvpe/` 目录下的权重文件 |

### 8.4 工程文件生成：三种格式、两种拼轨策略

支持的输出格式与对应构建方法：

| 格式 | 目标软件 | 关键方法 |
|---|---|---|
| `.svp` | Synthesizer V | `_build_svp_project_text` / `build_svp_project_text_multitrack` / `_build_svp_project_text_sequenced` |
| `.ustx` | OpenUtau | `_build_utau_project_text` / `build_utau_project_text_multitrack` / `_build_ustx_project_text_sequenced` |
| `.vsqx` | VOCALOID4 | `_build_vsqx_project_text` / `build_vsqx_project_text_multitrack` / `_build_vsqx_project_text_sequenced` |

每种格式都有 `multitrack`（多轨并行，用于对话批处理场景，每个 box 一条独立轨道）和
`sequenced`（多段依次拼接在同一条轨道，用于长音频分段场景）两种拼装策略。

**时间单位换算**是这部分代码的基础设施：

- Synthesizer V 用 **blick**（1 blick = 100 纳秒，绝对时间、与 BPM 无关），恰好与
  `.lab` 文件的时间戳单位（100ns）直接对应，无需换算；
- USTX 默认 **tick**（`_TICKS_PER_SECOND_DEFAULT = 480`，每四分音符 480 tick，与
  BPM 相关）；
- VSQX 走的是 `sec_to_ticks()`（`_build_vsqx_part_xml` 内部函数）做的秒→tick 换算。

**VSQX PIT（音高弯曲）曲线导出**是近期重点调试过的模块（据历史会话记录）：早期版本
在平滑/降采样 PIT 曲线时没有按音符边界切分，导致相邻音符交界处出现锯齿状/毛刺音高，
后来的修复把平滑操作限制在单个音符内部进行，不再跨音符边界平滑。

`AudioProcessingConfig`（dataclass，`tsubaki_processor.py:53-124`）是贯穿整条链路
的配置对象，字段覆盖 BPM、基准音高、F0 提取方法/设备/型号、平滑窗口、是否细化音高
（`refine_pitch`）、是否写入连续 F0 曲线（`export_pitch_line`）、VSQX PIT 曲线专属
平滑窗口、短休止符填充策略等，`to_dict()` 提供了与前端表单字段一一对应的序列化。

---

## 9. 多语言音素转换体系

音素转换分两层：

1. **`mfa_processor.py`（`MFAProcessor` 类）**：调用 MFA 命令行工具完成音频-文本对齐，
   并针对每种语言分别做词到音素的分配后处理——`_process_zh_words` / `_process_en_words`
   / `_process_ja_words` / `_process_ko_words` / `_process_yue_words` 五套独立逻辑，
   分别处理拼音无声调切分、ARPABET 分配、假名/片假名音拍分配、谚文（Hangul）音节
   分解、粤语 Jyutping 切分。日语部分还专门处理了片假名外来语读音校验
   （`_valid_katakana_reading`）等细节。

2. **`phoneme_converter.py`**：把 MFA 输出的 IPA 音素转换成目标合成引擎需要的音素集，
   核心是 `convert_phoneme()` / `convert_phoneme_list()` 加上一整套语言专属表：
   - 日语 IPA → 罗马字 → 平假名/片假名合并（`build_ja_hiragana_lab` /
     `build_ja_merged_lab`，含浊化辅音处理 `ja_devoiced_onset_to_vocaloid4`）；
   - 英语 IPA → ARPABET（`word_to_arpabet`，`g2p_en` 库 + 内置 MFA 词典兜底，
     `arpabet_to_vocaloid4` 做进一步的 VOCALOID4 专属映射）；
   - 数字展开为音素（`_expand_digits_to_phones`）；
   - IPA 变音符号剥离（供英语输出使用）。

3. **`dictionary_manager.py`**：用户自定义"单词→音素"词典系统，支持任意数量、
   任意命名的独立词典（不再局限于早期版本"synthesizerv"/"vocaloid"两个固定来源），
   每个词典创建时选择一个记号体系（notation，当前枚举为 `synthesizerv` /
   `vocaloid`），查找时大小写不敏感（`_find_case_insensitive_key`），支持 JSON/CSV
   批量导入导出。

---

## 10. 文本预处理引擎（text_processor.py）

服务于前端"优化文本"弹窗，模块头部注释明确设计原则："只负责纯文本→纯文本的字符串
转换，不落盘、不联网、不依赖对齐流程"，即完全无副作用的纯函数集合。核心能力按语言
（zh/en/ja/ko）区分实现：

- **数字转文字**：`int_to_zh` / `int_to_en` / `int_to_ja` / `int_to_ko` 四套独立数字
  转读法实现（中文含"万/亿"分组读法，日语含假名分组读法，韩语含固有数词分组）；
- **符号/百分比/温度/年份/分数**的语言相关转换（如"50%"→"百分之五十"/"fifty
  percent"）；
- **英文专属**：大小写转换、驼峰/连字符处理、单词间距规整；
- **换行规则**：按逗号/句号/每 N 句插入换行，服务于字幕分句等场景。

统一入口 `process_text(text, action, language, n)` 按 `action` 分发到具体转换函数，
供 `/api/text/optimize` 路由调用。

---

## 11. 字幕子系统

两个方向相反、互不依赖的模块：

### 11.1 字幕识别方向 — `subtitle_processor.py`

音频/视频 → 逐句字幕。流程：`extract_audio`（用 ffmpeg 抽取音轨）→
`vad_split_segments`（基于 RMS 能量曲线的语音活动检测切分）→
`transcribe_to_subtitles`（调用 Qwen3-ASR）→ `_close_small_gaps` /
`_merge_short_fragments`（合并过短碎片）→ 导出为 `export_srt` / `export_lrc` /
`export_txt` / `export_lab` 四种格式。另外还提供 `mux_soft_subtitles`（软字幕封装进
视频容器）和 `burn_subtitles_to_video`（硬字幕压制，走 ffmpeg 滤镜）。

### 11.2 字幕导入方向 — `subtitle_import.py`

与上面正好相反：用户已有 SRT/LRC 字幕 + 完整音频，把字幕时间轴当作"切分点+参考
文本"来源。`parse_srt` / `parse_lrc` / `parse_lab` 三种格式解析器统一产出
`SubtitleCue` 对象 → `build_timeline` 构建时间轴 → `_group_segments_for_alignment`
按需合并相邻片段（对应设置页的 `subtitle_align_group_size` 分组参数）→ 逐段
`_slice_wav` 切出音频小段 → `align_subtitle_audio` 对每段独立跑强制对齐，最终拼回
完整 `.lab`。这条链路是"字幕跟读"功能的后端支撑。

---

## 12. TTS 子系统

`tts_processor.py` 用一个 `ENGINES` 注册表统一抽象三种 TTS 引擎：

| 引擎 | 实现方式 | 平台限制 |
|---|---|---|
| Windows SAPI5 | `win32com` 调用系统自带语音合成 | 仅 Windows |
| EdgeTTS | `edge-tts` 库，微软在线 TTS | 需联网 |
| Qwen3-TTS | 通过 `:5003` 调用 `qwen3tts_server.py`，三种子模式：CustomVoice / VoiceDesign / VoiceClone | 需要独立 `qwen3tts_env` |

核心能力：

- `list_engines` / `check_available`：运行时探测各引擎是否可用（而非编译期硬编码）；
- `list_narrators` / `upsert_narrator` / `delete_narrator`：本地"讲述人"音色档案管理，
  支持保存参考音频（`ref_audio_base64`）供 VoiceClone 使用；
- `split_sentences` / `_find_split_point` / `_split_long_line`：中/英/日/韩混合文本的
  分句与长句拆分，服务于 TTS 逐句合成；
- `synthesize_and_align`：合成之后复用 `alt_aligners.py` 的强制对齐能力，给 TTS 输出
  的语音生成时间对齐的 `.lab`，还支持"对齐辅助音高偏移"——`_make_alignment_pitch_shifted_copy`
  用 `librosa` 对齐输入音频做半音偏移（仅用于提升对齐准确率，不影响最终 F0/输出音频）。

---

## 13. MIDI 处理

`midi_processor.py`（238 行，全仓最小的核心模块）提供双向能力：

- `parse_midi_notes` / `parse_midi_notes_with_lyrics`：从 MIDI 文件提取 BPM、
  音符时序/音高，以及可选的歌词事件（`MidiLyricEvent`）；
- `build_midi_from_segments` / `map_segment_to_midi_pitch`：把处理管线里的音素段
  映射回 MIDI 音符（供"仅生成工程"模式在"project-only"场景下，用 MIDI 音符音高替代
  F0 判定音高时使用，MIDI 自带 BPM 也可覆盖用户表单填写的 BPM）。

基于 `mido` 库实现。

---

## 14. 设置系统（app_settings.py）

单一 JSON 文件持久化（`backend/settings/app_settings.json`），核心能力：

- `apply_env_from_settings`：把设置里的 HuggingFace 相关开关同步成环境变量
  （`HF_HUB_OFFLINE` 离线模式、`HF_ENDPOINT` 镜像站地址），供依赖 HuggingFace 的
  Qwen3 系模型下载使用；
- `apply_console_visibility`：控制台窗口显示/隐藏开关，通过 `GetConsoleWindow()` 找到
  自身控制台句柄操作，三个后端服务可独立设置（对应 `launcher.py` 给每个子进程分配
  独立控制台的设计）；
- `get_alignment_tuning`：`alt_aligners.py` 里一批对齐后处理调优参数的读取入口
  （静音阈值、分块大小等），做到"不改代码、改设置页参数即可调优对齐效果"；
- `get_tts_segment_len` / `get_qwen3_batch_size` / `get_qwen3_tts_model_size` 等：
  各功能模块的可调参数集中在这里管理，前端 `SettingsPage.vue` 直接对应这些字段。

---

## 15. API 路由总表

`app.py` 暴露约 65 个 `/api/*` 端点，按功能分组：

| 分组 | 代表路由 | 说明 |
|---|---|---|
| 系统状态 | `/api/health`、`/api/debug/runtime`、`/api/mfa/status`、`/api/aligner/status`、`/api/pipeline/status` | 健康检查、各后端可用性探测 |
| 核心处理管线 | `/api/pipeline/full`、`/api/pipeline/mfa-only`、`/api/pipeline/project-only`、`/api/pipeline/f0-only`、`/api/pipeline/job/<id>`（含取消） | 五种处理模式的提交与轮询 |
| 对话批处理 | `/api/dialogue/process` | 多 box 批量处理 |
| 词典管理 | `/api/dictionary`、`/api/dictionary/<source>`（GET/POST/PATCH/DELETE）、`/api/dictionary/<source>/entry`、`/api/dictionary/<source>/export`、`/api/dictionary/<source>/import` | 完整 CRUD + 导入导出 |
| 文本工具 | `/api/text/optimize`、`/api/english/extract-g2p` | 文本预处理、英文 G2P 查询 |
| 设置 | `/api/settings`（GET/POST） | 全局设置读写 |
| TTS | `/api/tts/engines`、`/api/tts/status`、`/api/tts/voices`、`/api/tts/narrators`（CRUD）、`/api/tts/preview`、`/api/tts/synthesize_preview`、`/api/tts/process` | TTS 全流程 |
| 模型管理 | `/api/mfa/download-model/<language>`、`/api/f0/download-rmvpe` | 按需下载语言模型/RMVPE 权重 |
| 工作目录 | `/api/work-dir/files`、`/api/work-dir/download/<path>`、`/api/work-dir/clear` | 产物文件浏览/下载/清理 |
| 字幕识别 | `/api/subtitle/upload`、`/api/subtitle/recognize`、`/api/subtitle/job/<id>`、`/api/subtitle/export`、`/api/subtitle/split_entry`、`/api/subtitle/embed`、`/api/subtitle/embed-video`、`/api/subtitle/cleanup` | 识别→编辑→导出→封装/压制 全链路 |
| 字幕导入对齐 | `/api/subtitle-editor/import`、`/api/subtitle-import/split`、`/api/subtitle-import/slice/<sid>/<idx>`、`/api/subtitle-import/align`、`/api/subtitle-import/cleanup` | 字幕驱动的逐句对齐 |
| 底层对齐 | `/api/align`、`/api/mfa/process` | 单次对齐调用（不经过 job 队列的同步接口） |
| CLI 桥接 | `/api/cmd/exec` | 与命令行 `cmd` 子命令一一对应的 HTTP 版本，供外部脚本/工具链以网络调用方式触发同一批操作（详见 §16） |
| 静态资源 | `/`、`/<path:path>` | 前端 SPA 托管 |

所有耗时任务（对齐/F0/导出）都遵循"POST 提交拿 `job_id` → GET 轮询进度 → 完成后从
`/api/work-dir/download/<filename>` 取产物"的异步模式，`app.py` 里 `set_job` /
`get_job` / `request_job_cancel` / `is_job_cancel_requested` 维护一个进程内内存字典
作为极简任务状态机（无 Redis/数据库，重启即丢失，符合"本地单机小工具"的定位）。

---

## 16. 命令行入口（commandline.py）

> 完整的参数级用户手册见 [Docs/README_CLI.md](./README_CLI.md)；本节只梳理这条
> 支线在架构上是怎么接进主服务的，不重复罗列每个子命令的全部参数。

`commandline.py`（1,384 行）让 `app.py` 除了作为常驻 HTTP 服务被 `launcher.py`/
`run.bat` 拉起，还能被直接以命令行方式单次调用、执行完即退出。它不是一套独立实现，
而是**同一个 `AudioProcessingPipeline` 实例**的另一层调用入口——网页版
（`/api/pipeline/*` 路由）、CLI（`commandline.py`）、以及供外部脚本调用的
`/api/cmd/exec` 路由，三者最终都落到同一批 `pipeline.py`/`dictionary_manager.py`/
`app_settings.py`/`subtitle_processor.py` 函数上，不存在"网页一套逻辑、命令行另一套
逻辑、久而久之行为分叉"的问题。

### 16.1 三层调用入口

```
启动器.exe cmd ...                          （打包后，终端里敲）
        │  launcher.py: argv[1]=="cmd" 检测到，转发子进程（见 §2.2）
        ▼
runtime\mfa_env\python.exe backend\app.py cmd ...
        │  app.py: __main__ 里 commandline.is_cmd_mode() 检测到，
        │  不调用 main() 起 Flask 服务
        ▼
commandline.CmdUI(pipeline).run(sys.argv)   （真正的参数解析 + 调用 pipeline）


python app.py cmd ...                       （开发环境，跳过 launcher.py 直接跑）
        └─ 同样落到 CmdUI(pipeline).run(...)

POST /api/cmd/exec                          （HTTP，服务常驻时，供外部脚本调用）
        └─ app.py 路由内部把 JSON body 的 {"operation", "args"} 拼成 argv 列表，
           落到 CmdUI(pipeline).run_args(argv)
```

命令行模式**只依赖 `mfa_env`**：`launcher.py` 转发时固定用这一个环境的 Python
解释器，不会拉起 `qwen3_env`/`nemo_env`/`qwen3tts_env` 对应的三个微服务，需要用到
Qwen3-ASR/NeMo-FA/Qwen3-TTS 时（例如 `--aligner-backend qwen3_asr`、`asr-subtitle`
子命令、`dialogue-batch` 里的 Qwen3-TTS 跟读框），得由用户自己预先启动好对应服务
（正常双击 `启动器.exe` 走界面模式会自动拉起全部四个）。

### 16.2 子命令一览

`is_cmd_mode()` 判断 `argv[1] == "cmd"`；`argv[2]` 是子命令名，`CmdUI` 支持 13 个
（含别名）：

| 子命令 | 对应能力 | 底层调用 |
|---|---|---|
| `mfa-only`（`lab`） | 仅标注提取 | `pipeline.process_mfa_only()` |
| `f0-only`（`pitch`） | 仅音高提取，落盘 CSV | 直接调用底层 F0 提取函数（不经过 `pipeline.process_f0_only()`，见下方说明） |
| `project-only`（`project`） | 仅生成工程文件 | `pipeline.process_project_only()` |
| `full` | 标注+F0+工程文件一步到位 | `pipeline.process_full()` |
| `subtitle`（`sub`） | 字幕驱动的逐句强制对齐 | `subtitle_import` 系列函数 |
| `asr-subtitle`（`asr`/`subtitle-recognize`） | ASR 识别生成字幕 | `subtitle_processor.transcribe_to_subtitles()` + `export_subtitles()` |
| `dialogue-batch`（`dialogue`） | 多框批量处理（含 TTS 跟读预处理） | `pipeline.process_dialogue_batch()`（TTS 框先经 `tts_processor.synthesize_and_align()` 回填） |
| `dict-import`（`dict-load`）/`dict-list`/`dict-export`/`dict-edit` | 词典 CRUD | `dictionary_manager` 各函数 |
| `settings-get`/`settings-set` | 全局设置读写 | `app_settings.load_settings()`/`save_settings()` |

`f0-only` 是主线里唯一的例外：网页版 `pipeline.process_f0_only()` 只返回帧数/采样率
（供前端"测试"按钮展示概览用，不落盘曲线），命令行如果照搬这个方法用户就拿不到任何
产物，因此这个子命令改为直接调用同一份底层 F0 提取函数，算法与参数完全一致，只是
额外导出一份 `time_sec,freq_hz` 的 CSV。

### 16.3 与网页版共享的行为细节

- 产物默认落在 `backend/work/`（与网页版任务产物同一目录，复用既有清理/调试机制），
  `-o/--output` 只是"额外复制一份"到指定路径，不影响 `backend/work/` 里的原件；
- `--json` 开关在正常进度提示之外，额外打印一行机器可读 JSON（`success`/`error`
  加操作专属字段），供脚本化解析；退出码约定 `0` 成功 / `1` 运行时失败 /
  `2` 参数错误；
- 词典大小写规则、TTS 引擎可用性探测、对齐后端选择等均直接复用对应模块的既有函数，
  不重新实现一遍。

`/api/cmd/exec` 的请求体 `{"operation": ..., "args": {...}}` 里 `args` 的键名与命令
行参数一一对应（去掉 `-`/`--` 前缀、连字符转下划线），文件路径类参数
（`args.audio`/`args.lab`/`args.midi` 等）要求是**服务端本地已存在的路径**，不接收
文件上传——需要浏览器直传文件的场景仍然走 `/api/pipeline/*` 系列接口，两组并存、
互不替代。

---

## 17. 打包与分发

发布形态是一个免安装的绿色目录（见 `launcher.py` 文件头注释的目录布局），关键脚本：

- `pack_runtime.bat`：用 conda-pack 把 `mfa_env` / `qwen3_env` / `nemo_env`（以及
  `qwen3tts_env`，如果启用）打包成 `runtime/` 下的便携版 Python 环境，无需在目标机器
  重新安装依赖；
- `build_launcher.bat`：`pyinstaller --onedir --noconsole` 打包 `launcher.py` 本身
  （因为它不 import 重依赖，产物很小，与几百 MB～几 GB 的后端环境完全解耦）；
- `frontend/` 需要预先 `npm run build` 产出 `dist/`，随源码一起分发（不冻结进 exe）；
- `setup.bat`/`setup.sh` + `run.bat`/`run.sh` 是"源码直接运行"路径（开发者/进阶用户），
  与"绿色发布包 + launcher.exe"是两条并行的分发方式。

---

## 18. 依赖矩阵与环境隔离设计

四个 `requirements*.txt` 之间刻意零共享（除 Flask/numpy/soundfile/requests/tqdm 等
基础库外），每个文件头部都用大段注释说明"为什么不能装进同一个环境"：

| 环境 | 关键依赖 | 冲突根源 |
|---|---|---|
| `mfa_env` | `montreal-forced-aligner==3.3.9`、`whisperx>=3.2.0`、`torch==2.3.1+cpu`、`pyworld`、`torchcrepe` | 主环境，MFA 对 `kalpy` 等依赖有专属要求 |
| `qwen3_env` | `qwen-asr>=1.0.0`、`transformers>=4.40.0`、`torch==2.3.1+cpu` | 与主环境的 `transformers` 版本要求不同 |
| `nemo_env` | `nemo_toolkit[asr]>=2.7.0,<2.8.0`、`torch==2.6.0+cpu`（注意版本比主环境更新，因为 `forced_align`/`merge_tokens` 接口需要 `torchaudio>=2.1`） | NeMo 对 `packaging`/`fsspec`/`omegaconf`/`hydra-core`/`lightning` 有严格版本上限，会把其它工具需要的版本"降级" |
| `qwen3tts_env` | `qwen-tts`、`torch==2.3.1+cu121`（含 `torchvision`），Python 3.12 | Qwen3-TTS-Tokenizer-12Hz 需要比 `qwen3_env` 更新的 `transformers`，且官方推荐 Python 3.12 |

每个 GPU 相关依赖都保留了 CUDA 12.1 / CUDA 11.8 / CPU-only 三选一的注释块，用户按
自己的硬件取消注释对应行——项目默认发布配置是 CPU-only（对多数用户更省心，但意味着
重计算的 F0/对齐后端在无独立显卡时会明显慢）。

许可证方面，`ACKNOWLEDGMENT.md` 完整列出了每个第三方依赖的许可证类型（MIT/BSD-2/
BSD-3/Apache-2.0/LGPL 等），`Docs/LICENSES/` 存放对应许可证全文，项目自身以 MIT
许可证发布。

---

## 19. 工程亮点

几处体现出比"能跑就行"更进一步的工程考量，值得作为设计参考：

1. **微服务级依赖隔离**：用 HTTP 边界而不是包管理器解决"多个深度学习框架版本互斥"
   的老大难问题（详见 §2.1、§18）。
2. **子进程崩溃隔离**：`_run_pyworld_isolated` 针对原生扩展的操作系统级崩溃做了
   `multiprocessing` 隔离防护，避免单次 F0 提取失败拖垮整个服务（§8.2）。
3. **CUDA OOM/环境错误的统一探测与自动降级**：`alt_aligners.py`、`nemo_server.py`、
   `qwen3_server.py`、`qwen3tts_server.py` 都各自实现了 `_is_cuda_oom_or_env_error`，
   在显存不足或 CUDA 环境异常时自动回退到 CPU，而不是直接抛出让用户看不懂的堆栈。
4. **Windows 长路径安全**：`app.py` 里的 `WINDOWS_SAFE_PATH_LIMIT = 248` +
   `fit_stem_to_limit` / `sanitize_stem` 主动把生成的文件名裁剪到 Windows `MAX_PATH`
   安全范围内，避免用户上传长文件名后续环节静默失败。
5. **微服务自重启而不丢单请求语义**：`qwen3_server.py`/`nemo_server.py` 的 `/restart`
   路由用"先 `shutdown()`+`server_close()` 显式释放端口，再 `subprocess.Popen` 拉全新
   进程，旧进程 `os._exit(0)`"的顺序，规避了"端口未真正释放导致新进程 bind 失败"的
   竞态问题（`qwen3_server.py` 头部注释详细记录了这个坑）。
6. **任务可取消**：`app.py` 的 job 状态机支持 `request_job_cancel`，长任务（尤其是
   对话批处理的多 box 场景）执行中会定期检查 `is_job_cancel_requested`，用户可以中途
   打断而不用等全部跑完或杀掉整个进程。
7. **纯函数化的文本处理层**：`text_processor.py` 明确设计为无副作用纯函数集合，
   便于单独测试和复用（尽管目前仓库里还没有配套的自动化测试，见下一节）。

---

## 20. 已知技术债与可改进点

以下是通读代码后观察到、值得后续关注的点（非价值判断，仅作维护参考）：

- **没有自动化测试**：仓库内未发现 `tests/` 目录或任何测试框架配置（`pytest`/`unittest`
  均未出现在依赖或文件列表中）。对于 `alt_aligners.py` 这类算法密度很高、历史上反复
  出现过对齐质量 bug 的模块，补充针对分块规划、静音检测等纯函数的单元测试会显著
  降低回归风险。
- **超大单文件**：`alt_aligners.py`（5,258 行）、`app.py`（4,885 行）、
  `tsubaki_processor.py`（4,007 行）三个文件占后端总代码量的 47%。`app.py` 尤其
  混杂了路由定义、作业编排（`run_pipeline_job` / `run_dialogue_batch_job` /
  `run_tts_pipeline_job` 等大量业务逻辑直接写在路由处理函数里）和工具函数
  （路径安全、文件名处理），拆分成 `routes/`、`jobs/`、`utils/` 等子模块会提升可维护性。
- **任务状态存在内存中**：`app.py` 的 job 表是进程内字典，服务重启即丢失所有进行中
  任务的状态（对单机小工具场景影响有限，但如果未来考虑多进程/多副本部署会成为障碍）。
- **前端页面级状态管理**：9 个页面组件均独立管理自己的状态，`MFAProcessor.vue`
  （5,003 行）和 `DialogueBatch.vue`（4,594 行）作为单文件组件已经相当庞大，若后续
  功能持续增加，拆分为更小的子组件、或引入轻量状态管理会有助于长期维护。
- **CPU-only 为默认发布配置**：`requirements*.txt` 默认注释掉了所有 CUDA 相关行，
  多数深度学习后端（CREPE/RMVPE/WhisperX/Qwen3 系/NeMo）在无独立显卡的机器上会明显
  慢于同类工具的 GPU 加速版本，这是产品定位（易用优先）与性能之间的一处权衡取舍。

---

*本报告由静态阅读源码整理，未运行/未测试代码行为，如有理解偏差以实际源码为准。*
