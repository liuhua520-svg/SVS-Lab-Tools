# 命令行模式使用教程

SVS Lab Tools 现在支持在不打开界面、不启动常驻 HTTP 服务的情况下，用一条命令
完成「标注提取 / 音高提取 / 工程文件生成 / 完整处理 / 字幕跟读 / 字幕识别 /
对话批量处理 / 词典管理 / 全局设置」中的一个操作，方便写批处理脚本，或接入
其它命令行工具链。

本教程覆盖三个层面：

1. 打包后直接用 `启动器.exe cmd ...`（最终用户视角，无需装 Python/conda）
2. 开发环境下用 `python app.py cmd ...`（在 `.mfa_env` 里跑源码）
3. 已经启动着网页版服务时，用 HTTP 接口 `/api/cmd/exec` 做同样的事

涉及的三个文件：`launcher.py`（转发）、`backend/commandline.py`（命令行实现本体）、
`backend/app.py`（挂载 `cmd` 分流 + `/api/cmd/exec` 路由）。

---

## 1. 打包后使用（`启动器.exe cmd ...`）

前提：已经按 `build_launcher.bat` + `pack_runtime.bat` 打包好整个发布目录，
结构大致是：

```
YourApp/
├─ 启动器.exe
├─ _internal/
├─ backend/
│   ├─ app.py
│   ├─ commandline.py
│   └─ ...
├─ frontend/dist/
└─ runtime/
   ├─ mfa_env/       ← 命令行模式固定用这个环境
   ├─ qwen3_env/
   └─ nemo_env/
```

双击 `启动器.exe` 是正常的界面模式（托盘 + 原生窗口 + 三个后端服务）。
在**已经打开的命令行窗口**（cmd / PowerShell）里，把 `cmd` 作为第一个参数，
就会切换成命令行一次性调用模式：

```bat
cd YourApp
启动器.exe cmd mfa-only -a in.wav -t "参考文本" -o out.lab
```

行为上，`launcher.py` 检测到 `argv[1] == "cmd"` 后，**不会**启动托盘、
不会打开原生窗口、也不会拉起 qwen3/nemo 两个微服务，而是把整条命令原样转发给：

```
runtime\mfa_env\python.exe backend\app.py cmd mfa-only -a in.wav -t "参考文本" -o out.lab
```

子进程的 stdout/stderr、退出码都会原样透传回当前窗口，跑完就退出，
不占用 5000 端口。因此打包后命令行模式下的所有子命令、参数，和下面第 2 节
`python app.py cmd ...` 是完全一样的，把 `python app.py` 替换成 `启动器.exe` 即可。

> 命令行模式只依赖 `mfa_env`。如果 `runtime\mfa_env\python.exe` 不存在
> （比如 `pack_runtime.bat` 还没跑过），会直接打印错误提示并以退出码 1 结束，
> 不会误启动界面模式。

---

## 2. 开发环境使用（`python app.py cmd ...`）

在项目根目录，先激活/进入 `.mfa_env`（或者直接用它的 `python.exe`），
然后照常用 `python app.py cmd <operation> ...` 调用：

```bash
cd backend
python app.py cmd mfa-only -a in.wav -t "参考文本" -o out.lab
```

`app.py` 在 `if __name__ == "__main__":` 里会先判断 `commandline.is_cmd_mode()`
（同样是 `argv[1] == "cmd"`），命中就构造 `CmdUI` 跑完对应操作直接退出，
不会调用 `main()` 启动 Flask 服务、不会弹浏览器。

### 十三个子命令一览

| 子命令 | 别名 | 作用 | 对应网页版功能 |
|---|---|---|---|
| `mfa-only` | `lab` | 仅执行标注提取，产出 LAB 文件 | 「标注提取」 |
| `f0-only` | `pitch` | 仅执行音高(F0)提取，产出 CSV 曲线 | 「音高提取」（测试按钮的落盘版） |
| `project-only` | `project` | 已有 WAV + LAB/MIDI，仅生成工程文件 | 「工程文件输出」 |
| `full` | — | 标注提取 + 音高提取 + 工程文件生成，一步到位 | 「完整处理」 |
| `subtitle` | `sub` | 已有文本字幕，按 SRT/LRC/LAB 时间轴逐句强制对齐 | 「单文件处理 → 字幕跟读」 |
| `asr-subtitle` | `asr` / `subtitle-recognize` | 用 Qwen3-ASR 识别音频/视频，导出字幕文本文件 | 「字幕」页的识别 + 导出 |
| `dialogue-batch` | `dialogue` | 多段音频/TTS + 文本合并写入同一工程 | 「对话文本框批量处理」 |
| `dict-import` | `dict-load` | 从 CSV/JSON 导入词条到指定词典 | 「单词映射音素词典管理」的导入 |
| `dict-list` | — | 列出所有独立词典 | 同上，词典下拉列表 |
| `dict-export` | — | 导出指定词典为 CSV/JSON | 同上，导出 CSV/JSON |
| `dict-edit` | — | 增/删单个词条，或新建/删除/改名整个词典 | 同上，词典/词条的增删改名 |
| `settings-get` | — | 打印当前全局设置 | 「设置」页 |
| `settings-set` | — | 更新全局设置 | 「设置」页保存 |

`subtitle` 与 `asr-subtitle` 方向相反，容易搞混，注意区分：`subtitle`
是"已经有文本字幕（SRT/LRC/LAB），只是没有精确到音频的时间轴，靠强制
对齐补上"；`asr-subtitle` 是"什么都没有，音频里说的什么话完全靠 Qwen3-ASR
识别出来"。

通用规则：

- `--json` 加在子命令前后都可以，比如这两种写法等价：
  ```bash
  python app.py cmd --json mfa-only -a in.wav -t "文本"
  python app.py cmd mfa-only -a in.wav -t "文本" --json
  ```
  加了 `--json` 之后，除了正常的进度提示，还会在最后额外打印一行机器可读的
  JSON 结果（方便脚本 `| jq` 之类解析）。
- 想看某个子命令的完整参数列表，用 `--help`：
  ```bash
  python app.py cmd mfa-only --help
  python app.py cmd f0-only --help
  python app.py cmd project-only --help
  python app.py cmd full --help
  ```
- `-o/--output`：各子命令产出的文件本来就会落在 `backend/work/` 工作目录下
  （和网页版一致，方便复用已有的清理/调试机制）。传了 `-o` 的话，会在此基础上
  **再复制一份**到你指定的路径，不影响 `backend/work/` 里原有的产物。
- 退出码：`0` 成功；`1` 运行时失败（比如文件不存在、对齐失败）；
  `2` 参数错误（缺必填参数、枚举值不认识等，argparse 自身的约定）。

### 2.1 标注提取（`mfa-only` / `lab`）

只跑对齐，产出 `.lab` 标注文件，不生成工程文件。

```bash
# 默认用 MFA 后端
python app.py cmd mfa-only -a in.wav -t "参考文本" -o out.lab

# 用 WhisperX 做纯 ASR（不需要参考文本）
python app.py cmd mfa-only -a in.wav --aligner-backend whisperx -o out.lab

# 指定语言、开启英语单词级对齐
python app.py cmd mfa-only -a in.wav -t "hello world" -l eng --english-word-align -o out.lab
```

常用参数：

| 参数 | 说明 | 默认值 |
|---|---|---|
| `-a, --audio` | 输入音频路径 (wav)，**必填** | — |
| `-t, --text` | 参考文本（与 `-T` 二选一） | — |
| `-T, --text-file` | 从文件读参考文本 (utf-8) | — |
| `-l, --language` | 语种代码 | `cmn` |
| `--aligner-backend` | `mfa` / `whisperx` / `qwen3_asr` / `qwen3_aligner` / `nemo_aligner` | `mfa` |
| `--aligner-device` | 对齐后端运行设备，不传则跟随 `--f0-device` | 跟随 `--f0-device` |
| `--f0-device` | `auto` / `cpu` / `cuda` | `auto` |
| `--whisperx-model` | `large-v3` / `large-v3-turbo` / `large-v2` / `medium` / `small` / `base` / `tiny` | `large-v3` |
| `--whisperx-batch-size` | WhisperX 推理批大小（仅 `--aligner-device cuda` 时有意义） | `16` |
| `--qwen3-batch-size` | Qwen3-ASR / Qwen3-FA / NeMo-FA 共用批大小 | `8` |
| `--nemo-model` | NeMo Forced Aligner 模型覆盖（可选） | 按语言用内置默认模型 |
| `--english-word-align` | 启用英语单词级对齐（开关） | 关闭 |
| `--align-pitch-shift` | 对齐辅助移调（半音，`-24`~`24`） | `0.0` |
| `-o, --output` | LAB 输出路径 | 只留在 `backend/work/` |

> `mfa`、`qwen3_aligner`、`nemo_aligner` 是强制对齐，必须提供 `-t`/`-T`；
> `whisperx`、`qwen3_asr` 支持纯 ASR，文本可选。

### 2.2 音高提取（`f0-only` / `pitch`）

只提取 F0 曲线，落盘成 `time_sec,freq_hz` 两列的 CSV。

```bash
python app.py cmd f0-only -a in.wav -o out.csv
python app.py cmd f0-only -a in.wav --method crepe --crepe-model tiny --f0-device cuda -o out.csv
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `-a, --audio` | 输入音频路径 (wav)，**必填** | — |
| `--method` | `dio` / `harvest` / `crepe` / `rmvpe` | `dio` |
| `--f0-floor` | 最低基频 (Hz) | `71.0` |
| `--f0-ceil` | 最高基频 (Hz) | `800.0` |
| `--no-smooth` | 关闭平滑（开关） | 默认开启平滑 |
| `--smooth-window` | 平滑窗口帧数 | `5` |
| `--double-precision` | 双精度计算（开关） | 关闭 |
| `--f0-device` | `auto` / `cpu` / `cuda` | `auto` |
| `--crepe-model` | `full` / `tiny`（仅 `--method crepe` 时生效） | `full` |
| `-o, --output` | CSV 输出路径 | 音频同目录 `<音频文件名>.f0.csv` |

> 这个子命令**不是**直接调用网页版背后的 `pipeline.process_f0_only()`
> ——那个方法只返回帧数/采样率，不落盘曲线文件（是给网页「测试」按钮
> 展示概览用的）。命令行下如果什么文件都不落盘，用户等于拿不到任何产物，
> 所以这里换成直接调用同一份底层提取函数，算法和参数含义完全一致，
> 只是多导出一份 CSV。

### 2.3 工程文件输出（`project-only` / `project`）

已经有 WAV，以及 LAB 或 MIDI（至少一个），只生成工程文件（不重新跑对齐/不重新提取音高）。

```bash
# 从 LAB 生成 Synthesizer V 工程
python app.py cmd project-only -a in.wav --lab in.lab -f sv -o out.svp

# 从 MIDI 生成 VSQX 工程，指定声库
python app.py cmd project-only -a in.wav --midi in.mid -f vsqx \
    --vsqx-singer MIKU_V4_Chinese --vsqx-singer-id BNGE7CP7EMTRSNC3 --vsqx-singer-bs 4 \
    -o out.vsqx
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `-a, --audio` | 输入音频路径 (wav)，**必填** | — |
| `--lab` | LAB 标注文件路径（与 `--midi` 至少提供一个） | — |
| `--midi` | MIDI 文件路径（与 `--lab` 至少提供一个） | — |
| `-f, --format` | `sv` / `ustx` / `utau` / `midi` / `vsqx`（含别名 `svp`/`openutau`/`vocaloid` 等） | `sv` |
| `--title` | 工程标题 | `Project` |
| `-l, --language` | 语种（可选，供工程构建器判断，不传则按内容自动判断） | — |
| `--bpm` | BPM | `120.0` |
| `--base-pitch` | 基准音高 (MIDI note) | `60` |
| `--f0-method` / `--f0-floor` / `--f0-ceil` / `--no-smooth` / `--smooth-window` / `--double-precision` / `--f0-device` / `--crepe-model` | 同「音高提取」一节 | 同上 |
| `--vsqx-pitch-smooth-window` | VSQX PIT 曲线平滑窗口 | `5` |
| `--auto-note-pitch` | 用 F0 中位音高细化音符音高（开关） | 关闭 |
| `--export-pitch-line` | 将 F0 曲线写入工程文件（开关） | 关闭（与网页版一致） |
| `--phoneme-mode` | `none` / `merge` / `hiragana` / `katakana` | `none` |
| `--lyrics-text` | 歌词文本 | `""` |
| `--original-text` | 原始歌词文本，用于预提取英语单词（可选） | `""` |
| `--vsqx-singer` / `--vsqx-singer-id` / `--vsqx-singer-bs` | VSQX 声库名/ID/Bank Select，不传则按 `--language` 自动选择 | 自动（日语声库） |
| `--word-phoneme-map` | 英语单词 → 音素写入 SVP/VSQX（开关） | 关闭 |
| `--dict-source` | 单词→音素词典来源 | `default` |
| `--ja-devoiced-phoneme` | 日语辅音起始音素锁定（开关） | 关闭 |
| `--fill-short-rests` | 自动填充短休止符（开关） | 关闭 |
| `--fill-short-rests-max-length` | 判定"短"的音符时值阈值：`8`/`16`/`32`/`64`/`128` | `16` |
| `-o, --output` | 工程文件输出路径 | 只留在 `backend/work/` |

### 2.4 完整处理（`full`）

标注提取 + 音高提取 + 工程文件生成，一条命令走完整个流程，等价于网页版的「完整处理」。

```bash
python app.py cmd full -a in.wav -t "参考文本" -f sv -o out.svp

python app.py cmd full -a in.wav -t "参考文本" -f vsqx \
    --aligner-backend whisperx --whisperx-model large-v3-turbo \
    --auto-note-pitch -o out.vsqx --json
```

参数是「标注提取」+「工程文件输出」两节参数的并集（`-t`/`-T` 必填，
`--aligner-*`/`--whisperx-*`/`--qwen3-*`/`--nemo-model`/`--english-word-align`
来自标注提取那部分；`-f`/`--title`/`--bpm`/`--f0-*`/`--vsqx-*` 等来自工程文件
那部分），完整列表见：

```bash
python app.py cmd full --help
```

有两处和前两个子命令名字相同但含义略有差别，注意一下：

- `--aligner-device` 默认值是 `auto`（`mfa-only` 子命令里默认是"跟随 `--f0-device`"，
  这里是独立的 `auto`）。
- `--no-pitch-line`：这里默认**写入** F0 曲线，加这个开关才关闭；
  和 `project-only` 子命令里 `--export-pitch-line` 默认**不写入**、加开关才打开，
  正好相反——这是为了和网页版「完整处理」/「仅生成工程」两个模式各自的默认行为保持一致。

### 2.5 字幕跟读（`subtitle` / `sub`）

上传一份完整音频 + 一份 SRT/LRC/LAB 字幕，按字幕时间轴逐句固定用
**Qwen3-ForcedAligner** 强制对齐，产出覆盖整段音频的 LAB；等价于网页版
「单文件处理 → 字幕跟读」。默认只产出 LAB（等价网页版「仅标注(快速)」），
加 `--full` 才继续生成工程文件（等价网页版「完整处理」）。字幕跟读固定
使用 Qwen3-ForcedAligner，没有 `--aligner-backend` 选项。

```bash
# 仅产出 LAB
python app.py cmd subtitle -a in.wav -sf "字幕.srt" -o out.lab

# 完整处理，额外生成工程文件
python app.py cmd subtitle -a in.wav -sf "字幕.lrc" --full -f vsqx -o out.vsqx --json
```

常用参数：

| 参数 | 说明 | 默认值 |
|---|---|---|
| `-a, --audio` | 输入音频路径，**必填** | — |
| `-sf, --subtitle-file` | SRT / LRC / LAB 字幕文件路径，**必填**（格式按扩展名优先、内容兜底自动判断） | — |
| `-l, --language` | 语种代码 | `cmn` |
| `--aligner-device` | Qwen3-ForcedAligner 运行设备 | `auto` |
| `--english-word-align` | 启用英语单词级对齐（开关） | 关闭 |
| `--align-pitch-shift` | 对齐辅助移调（半音） | `0.0` |
| `--skip-split-every-n` | 每 N 条字幕合并成一个对齐块；不传则读取设置页保存的全局值 | 设置页的值（默认 1） |
| `--full` | 额外生成工程文件（开关）；不传则只产出 LAB | 关闭 |
| `-o, --output` | LAB（默认）或工程文件（`--full` 时）输出路径 | 只留在 `backend/work/` |

`--full` 时还可以传 `-f/--format`、`--title`、`--bpm`、`--f0-*`、
`--vsqx-*`、`--word-phoneme-map`、`--dict-source` 等，语义与「工程文件
输出」一节完全一致（`--export-pitch-line` 默认开启，用 `--no-pitch-line`
关闭），完整列表见 `python app.py cmd subtitle --help`。

> `-sf/--subtitle-file` 这里的 LAB 格式，指的是"整句一行"的字幕级 LAB
> （`asr-subtitle` 导出 `-f lab` 产出的就是这种），不是 MFA/Qwen3-FA
> 强制对齐产出的音素级 LAB——两者虽然扩展名都是 `.lab`，但每行含义不同，
> 解析时会按"起始 结束 文本"三段式识别，纯静音的 `SIL` 行会被跳过。

> 与「对话文本框批量处理 → 导入字幕」是两个不同功能：那个是把整段音频
> 切成多个独立对话框、分别可编辑后再批量处理（对应 `dialogue-batch`
> 子命令）；这里是直接产出一份覆盖整段音频的连续 LAB/工程文件，不拆分
> 成多个片段。

### 2.6 对话文本框批量处理（`dialogue-batch` / `dialogue`）

多段音频/台词 + 文本，最终合并写入*同一个*工程文件的*同一条音轨*（每段
是该音轨下一段独立序列，按顺序背靠背排列），等价网页版「对话文本框
批量处理」。每个对话框可以是**音频跟读**（现成的 wav）或 **TTS 跟读**
（只给文本 + 音色，音频当场用 EdgeTTS/Windows SAPI/Qwen3-TTS 合成，
再固定用 Qwen3-ForcedAligner 对齐）之一，两种框可以在**同一份**
`--manifest` 清单里混用；`--folder` 目录扫描只支持音频跟读（TTS 跟读
没有现成音频文件可扫描）。

两种输入方式，二选一（同时提供时 `--manifest` 优先）：

```bash
# 方式一：JSON 清单，精确控制每个对话框，音频跟读/TTS 跟读可以混用
python app.py cmd dialogue-batch --manifest boxes.json -f sv -o out.svp

# 方式二：文件夹，按同名文件自动配对（仅音频跟读）
python app.py cmd dialogue-batch --folder ./对话素材 -f vsqx -o out.vsqx
```

`boxes.json` 格式（数组，每项描述一个对话框，`audio` 与 `tts` 二选一）：

```json
[
  {"text": "你好", "audio": "你好.wav"},
  {"audio": "再见.wav", "lab": "再见.lab", "align_pitch_shift": 2.0},
  {"tts": {"engine": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural", "text": "第三句，TTS合成"}},
  {"tts": {"engine": "qwen3_tts", "text": "第四句，克隆音色",
           "qwen3_tts_options": {"mode": "voice_clone", "ref_audio_path": "参考音色.wav", "ref_text": "参考文本"}}}
]
```

**音频跟读框字段：**

- `text`：台词文本，可选（对齐后端为 `whisperx`/`qwen3_asr` 时可以不提供，走纯 ASR）。
- `audio`：现成的音频文件路径，**必填**（与 `tts` 二选一）。
- `lab`：已有 LAB 标注，提供则该框跳过对齐，直接使用（优先级高于 `midi`）。
- `midi`：已有 MIDI，无 `lab` 时提供则跳过对齐，从 MIDI 音符自动生成段落。
- `align_pitch_shift`：该对话框自己的对齐辅助移调（半音），默认 `0.0`。

**TTS 跟读框字段（`tts` 对象）：** 音频当场合成，固定用 Qwen3-ForcedAligner
对齐（不受 `--aligner-backend` 影响），语言/设备/英语单词级对齐沿用整批
统一的 `-l/--language`/`--aligner-device`/`--english-word-align`。

| 字段 | 说明 | 默认值 |
|---|---|---|
| `text` | 台词文本，**必填**（这里没给的话，回退用外层同框的 `text` 字段） | — |
| `engine` | `edge_tts` / `windows_sapi` / `qwen3_tts` | `edge_tts` |
| `voice` | 音色 id；`engine="qwen3_tts"` 且模式是 `voice_design`/`voice_clone` 时不需要 | — |
| `rate` / `pitch` / `volume` | 语速/音调/音量（如 `"+10%"`/`"+0Hz"`），`engine="qwen3_tts"` 时忽略 | `"+0%"`/`"+0Hz"`/`"+0%"` |
| `qwen3_tts_options` | 仅 `engine="qwen3_tts"` 时读取，见下表 | — |

`qwen3_tts_options` 对象（仅 `engine="qwen3_tts"`）：

| 字段 | 说明 |
|---|---|
| `mode` | `custom_voice`（默认，需要 `voice`）/ `voice_design`（需要 `instruct`）/ `voice_clone`（需要 `ref_audio_path` 或 `ref_audio_base64`） |
| `size` | 模型规格，如 `"1.7B"` |
| `device` | `auto`/`cpu`/`cuda` |
| `instruct` | 声音描述文本，`mode="voice_design"` 时必填，`custom_voice` 下可选（追加音色微调指令） |
| `ref_text` | 参考音频对应的文本，`mode="voice_clone"` 时用于提升克隆质量（可选） |
| `ref_audio_path` | 参考音频的**本地文件路径**，`mode="voice_clone"` 时必填之一（命令行直接给路径即可，不像网页版需要走文件上传） |
| `ref_audio_base64` / `ref_audio_ext` | 参考音频改用 base64 内联提供时的替代写法，与 `ref_audio_path` 二选一 |
| `x_vector_only` | 布尔，`mode="voice_clone"` 时是否只用音色特征、不用参考文本做韵律克隆 |

> TTS 跟读固定依赖对应的服务已经在运行：`engine="edge_tts"`/`"windows_sapi"`
> 不需要额外服务；`engine="qwen3_tts"` 需要 `qwen3tts_server.py`（与
> `asr-subtitle` 依赖 `qwen3_server.py` 是两个不同的微服务，见 2.7 节的
> 同类说明）。命令行下某个对话框 TTS 合成/对齐失败不会中断整批处理：
> 失败的框会被跳过并计入返回结果的 `failed_count`/`boxes`，其余对话框
> 照常继续。

`--folder DIR` 会扫描目录，按"去扩展名后的文件名"配对同名的音频（wav/mp3/flac/m4a/aac/ogg/wma/opus）
与 `.lab`/`.mid`/`.midi`/`.txt` 文件——与网页版"导入文件夹（按文件名自动
配对）"规则一致：同名 `.lab` 与 `.mid`/`.midi` 同时存在时优先使用
`.lab`；同名 `.txt` 存在则读作台词文本；没有配对到音频的文件（孤立的
`.lab`/`.txt`）不会单独成框。按文件名排序，结果可复现。

常用参数：

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--manifest` | JSON 清单文件路径 | — |
| `--folder` | 目录路径，自动配对（仅音频跟读） | — |
| `-l, --language` | 语种代码 | `cmn` |
| `-f, --format` | `sv` / `vsqx` / `ustx`（不支持 MIDI 标准文件，单音轨概念不适用） | `sv` |
| `--title` | 工程文件名 | `Dialogue Project` |
| `--processing-mode` | `full`（没有 LAB/MIDI 的对话框走对齐）或 `project-only`（仅用已提供的 LAB/MIDI，其余跳过）；含 TTS 跟读框时固定按 `full` | `full` |
| `--phoneme-mode` | `none`/`merge`/`hiragana`/`katakana` | `none` |
| `--aligner-backend` | 音频跟读框走对齐时使用的后端（TTS 跟读框固定 Qwen3-ForcedAligner，不受此参数影响） | `mfa` |
| `-o, --output` | 工程文件输出路径 | 只留在 `backend/work/` |


其余 `--bpm`/`--f0-*`/`--vsqx-*`/`--word-phoneme-map`/`--dict-source`/
`--fill-short-rests`/`--fill-short-rests-max-length` 等参数语义与「工程
文件输出」一节一致，完整列表见 `python app.py cmd dialogue-batch --help`。

### 2.7 字幕识别（`asr-subtitle` / `asr` / `subtitle-recognize`）

上传一份音频或视频，用 **Qwen3-ASR** 识别出里面说的话，自动切句并配上
时间轴，导出成 SRT/LRC/TXT/LAB 字幕文本文件；等价于网页版「字幕」页面
「上传 → 识别 → 导出」这条主链路（去掉了逐条手动编辑/拆分/内嵌回视频
这些交互式功能，命令行只覆盖"识别 → 拿到文本文件"这个最核心的用途）。

与 `subtitle`（字幕跟读）方向相反：`subtitle` 是已经有文本字幕、只是
没有精确时间轴；这里是完全没有文本，靠 ASR 识别出来。

```bash
# 基本用法，默认输出到同目录同名 .srt
python app.py cmd asr-subtitle -a interview.mp4

# 指定语言、导出 LRC、按句末标点二次拆分
python app.py cmd asr-subtitle -a song.wav -l zh -f lrc --split-at-sentence-end -o out.lrc
```

常用参数：

| 参数 | 说明 | 默认值 |
|---|---|---|
| `-a, --audio` | 输入音频或视频文件路径，**必填** | — |
| `-l, --language` | 语言代码，`auto` 或 `zh`/`en`/`ja`/`ko`/`fr`/`de`/`es`/`ru` 等 | `auto` |
| `--device` | `auto`/`cpu`/`cuda`，转发给 qwen3_server.py | `auto` |
| `--batch-size` | Qwen3-ASR 推理批大小 | `8` |
| `--max-chars` | 单条字幕最大字符数，超过则按标点二次拆分 | 内置默认值（34） |
| `--split-at-sentence-end` | 无条件遇到句末标点就切成下一条字幕（开关） | 关闭 |
| `--allow-comma-split` | 逗号/顿号也当作切分点，仅 `--split-at-sentence-end` 开启时生效（开关） | 关闭 |
| `--remove-punctuation` | 识别结果中移除标点符号（开关） | 关闭 |
| `--close-vad-gaps` | 相邻字幕间隔过大时对半分配到中点，让时间轴更紧凑（开关） | 关闭 |
| `--vad-gap-threshold` | 触发 `--close-vad-gaps` 的间隔下限（秒） | `0.6` |
| `-f, --format` | `srt`/`lrc`/`txt`/`lab` | `srt` |
| `-o, --output` | 字幕输出路径 | 音频同目录 `<stem>.<format>` |

> 固定依赖 `qwen3_server.py` 已经在运行（通过 HTTP 调用
> `127.0.0.1:5001/asr`）。正常双击 `启动器.exe` 的界面模式会自动拉起这个
> 微服务；命令行模式不会替你启动，服务未就绪时会在开始识别前就检测到
> 并给出明确报错（而不是卡住等 HTTP 超时）。

### 2.8 词典管理（`dict-import` / `dict-list` / `dict-export` / `dict-edit`）

对应网页版「单词映射音素词典管理」页面的全部功能：导入/列表/导出是
整份文件级别的操作，`dict-edit` 是单个词条 / 词典本身的增删改。

```bash
# 导入（词典不存在则自动创建）
python app.py cmd dict-import mydict --file entries.csv
python app.py cmd dict-import mydict --file entries.json --notation vocaloid

# 列出所有词典
python app.py cmd dict-list

# 导出（不传 -o 则打印到 stdout）
python app.py cmd dict-export mydict --format csv -o mydict.csv
python app.py cmd dict-export mydict --format json
```

CSV 格式：首行表头 `word,phonemes`，其后每行一个词条。JSON 支持两种
形状：扁平 `{"WORD": "phones", ...}`，或 `dict-export --format json`
产出的 `{"词典名": {"notation": "...", "entries": {...}}}`。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `name`（位置参数） | 词典名 | — |
| `--file` | 要导入的 `.csv` 或 `.json` 文件路径（`dict-import` 必填） | — |
| `--notation` | `synthesizerv` / `vocaloid`，仅在自动创建新词典时生效 | `synthesizerv` |
| `--no-overwrite` | 已存在的同名单词（大小写不敏感）跳过而不是覆盖 | 默认覆盖 |
| `--format`（`dict-export`） | `csv` / `json` | `csv` |
| `-o, --output`（`dict-export`） | 不传则打印到 stdout | — |

**`dict-edit`：不需要准备文件，直接增/删一个词条，或新建/删除/改名
整个词典**

```bash
# 新增/更新一个词条（词典不存在会自动创建）
python app.py cmd dict-edit mydict set --word HELLO --phonemes "hh ah l ow"

# 删除一个词条
python app.py cmd dict-edit mydict remove --word HELLO

# 新建一本空词典
python app.py cmd dict-edit mydict2 create --notation vocaloid

# 整本词典改名 / 删除
python app.py cmd dict-edit mydict2 rename --new-name mydict3
python app.py cmd dict-edit mydict3 delete
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `name`（位置参数） | 词典名 | — |
| `action`（位置参数） | `set`/`remove`/`create`/`delete`/`rename` | — |
| `--word` | 单词（`action=set`/`remove` 时必填） | — |
| `--phonemes` | 音素序列，如 `"hh ah l ow"`（`action=set` 时必填，记号规则不强制校验，原样存储） | — |
| `--notation` | `synthesizerv` / `vocaloid`（`action=create` 时使用；对已存在的词典无效，记号体系创建后不可更改） | `synthesizerv` |
| `--new-name` | 新词典名（`action=rename` 时必填） | — |

> 单词按精确大小写匹配删除（`hello` 和 `HELLO`视为不同词条的删除目标），
> 但新增/更新时若词典里已存在仅大小写不同的同一单词，会就地替换成这次
> 的大小写写法——这一点和网页版词典管理页面的行为完全一致。

### 2.9 全局设置（`settings-get` / `settings-set`）

对应网页版「设置」页。

```bash
python app.py cmd settings-get

python app.py cmd settings-set --set hf_hub_offline=true --set download_mirror=false
```

`--set key=value` 可重复传入；`value` 会按 `true`/`false` → 布尔、整数、
小数、字符串的顺序自动推断类型。具体有哪些设置项、每项的含义，参考网页版
「设置」页面上的说明文字（`settings-get` 打印出的 JSON 键名与网页版表单
字段一一对应）。

> 命令行下 `settings-set` **不会**像网页版那样自动尝试重启正在运行的
> Qwen3-ASR / NeMo Forced Aligner 微服务——那两个是独立进程，命令行这次
> 调用和它们没有进程间关联。需要让 `HF_HUB_OFFLINE`/`HF_ENDPOINT` 之类
> 设置对已运行的微服务生效，请用网页设置页保存（会自动重启），或手动
> 重启对应服务。

---

## 3. HTTP 接口（`/api/cmd/exec`）

如果 `app.py` 已经作为常驻服务跑起来了（无论是被 `启动器.exe` 拉起，还是
`python app.py` 直接跑），也可以不走命令行，直接发一个 HTTP 请求触发同样的操作，
拿到 JSON 结果——适合让别的工具/脚本通过网络调用，而不是拼命令行字符串再解析
子进程输出。

```
POST /api/cmd/exec
Content-Type: application/json
```

请求体：

```json
{
  "operation": "mfa-only",
  "args": {
    "audio": "backend/work/in.wav",
    "text": "参考文本",
    "output": "backend/work/out.lab"
  }
}
```

- `operation`：`mfa-only`（含别名 `lab`）/ `f0-only`（含别名 `pitch`）/
  `project-only`（含别名 `project`）/ `full` / `subtitle`（含别名 `sub`）/
  `asr-subtitle`（含别名 `asr`、`subtitle-recognize`）/
  `dialogue-batch`（含别名 `dialogue`）/ `dict-import`（含别名 `dict-load`）/
  `dict-list` / `dict-export` / `dict-edit` / `settings-get` /
  `settings-set`，和命令行子命令名完全一致。
- `args`：键名和上面几节表格里「去掉 `-`/`--` 前缀、下划线形式」的参数名一一对应
  （比如命令行的 `--align-pitch-shift` 对应这里的 `"align_pitch_shift"`），
  值直接传 JSON 原生类型；布尔开关型参数（`english_word_align` / `no_smooth` /
  `double_precision` / `auto_note_pitch` / `export_pitch_line` / `no_pitch_line` /
  `word_phoneme_map` / `ja_devoiced_phoneme` / `fill_short_rests` / `full` /
  `no_overwrite` / `split_at_sentence_end` / `allow_comma_split` /
  `remove_punctuation` / `close_vad_gaps`）传 `true`/`false`，`false` 或不传都
  等价于命令行里不加这个开关；位置参数（`dict-import`/`dict-export`/`dict-edit`
  的 `name`，`dict-edit` 还多一个 `action`）按普通字段传即可，多个位置参数按
  它们在命令行里出现的顺序书写，例如
  `"args": {"name": "mydict", "action": "set", "word": "HELLO", "phonemes": "hh ah l ow"}`
  （`name` 写在 `action` 前面，和命令行 `dict-edit mydict set ...` 的顺序一致）。
- `settings-set` 是唯一的例外：命令行版的 `--set key=value` 是可重复参数，
  与其它操作"每个参数名对应一个 `--参数名 value`"的一对一映射规则不同，
  HTTP 版本改为嵌套写法——`args` 只有一个 `updates` 字段，值是一个
  `{设置项: 新值}` 的对象，例如：
  ```json
  {
    "operation": "settings-set",
    "args": {"updates": {"hf_hub_offline": true, "download_mirror": false}}
  }
  ```
  等价于命令行的 `settings-set --set hf_hub_offline=true --set download_mirror=false`。

**和 `-a`/`--audio` 等文件路径参数的关键区别**：这个接口不接收文件上传，
`args.audio` / `args.lab` / `args.midi` 必须是**服务端本地已经存在的路径**
（比如之前 `/api/pipeline/*` 系列接口落盘到 `backend/work/` 下的文件，
或者服务端本机磁盘上的任意可访问路径）。如果要从浏览器直接上传文件处理，
应该用 `/api/pipeline/mfa-only`、`/api/pipeline/full` 等已有的「文件上传 +
异步任务轮询」系列接口，那一组和这里并存，互不替代。

返回值就是对应命令行子命令 `--json` 会打印的那份结果 dict（`success` /
`error` / `lab_path` / `project_path` / `final_output_path` 等字段），
HTTP 状态码：`200` 成功；`422` 业务失败（`success: false` 但请求本身合法）；
`400` 参数错误（`operation` 缺失，或 `args` 缺必填字段/枚举值不认识）；
`500` 未预期的异常。

curl 示例：

```bash
curl -X POST http://127.0.0.1:5000/api/cmd/exec \
  -H "Content-Type: application/json" \
  -d '{
        "operation": "full",
        "args": {
          "audio": "backend/work/in.wav",
          "text": "参考文本",
          "format": "sv",
          "output": "backend/work/out.svp"
        }
      }'
```

---

## 4. 三层是怎么串起来的（供后续维护参考）

```
启动器.exe cmd ...                          （打包后，终端里敲）
        │  launcher.py: argv[1]=="cmd" 检测到，转发子进程
        ▼
runtime\mfa_env\python.exe backend\app.py cmd ...
        │  app.py: __main__ 里 commandline.is_cmd_mode() 检测到
        ▼
commandline.CmdUI(pipeline).run(sys.argv)   （真正的参数解析 + 调用 pipeline）


python app.py cmd ...                       （开发环境，直接跑）
        │  app.py: __main__ 里同样先判断 is_cmd_mode()
        ▼
commandline.CmdUI(pipeline).run(sys.argv)   （和上面走到同一处）


POST /api/cmd/exec                          （HTTP，服务常驻时）
        │  app.py 路由内部：JSON args → argv 列表
        ▼
commandline.CmdUI(pipeline).run_args(argv)  （同一个 CmdUI，同一份 pipeline 实例）
```

三条路径最终都落到同一个 `CmdUI` 实例、同一个 `pipeline`（`AudioProcessingPipeline`）
对象上，参数含义、默认值、落盘位置完全一致，不存在"网页版一套逻辑、命令行版
另一套逻辑，久而久之行为分叉"的问题。`commandline.py` 内部对 `mfa-only` /
`project-only` / `full` 三个操作也是直接调用 `pipeline.process_mfa_only()` /
`process_project_only()` / `process_full()`，和 `/api/pipeline/*` 系列网页路由
背后调用的是同一批方法。

`f0-only` 是唯一在"标注/工程文件"这条主线里的例外：它不调用
`pipeline.process_f0_only()`（原因见 2.2 节末尾的说明），而是直接调用
其内部同一份 F0 提取函数，提取算法本身仍然一致，只是命令行版多导出了
一份 CSV 曲线文件。

`asr-subtitle` 不经过 `pipeline` 对象，而是直接调用
`subtitle_processor.transcribe_to_subtitles()` +
`subtitle_processor.export_subtitles()`——这两个函数本来就是
`/api/subtitle/recognize` + `/api/subtitle/export` 两个路由背后的实现，
`commandline.py` 只是去掉了它们之间"前端持有 entries、用户点击导出"
这一步人工交互，识别完立即导出成文件。

`dict-edit`/`dict-import`/`dict-list`/`dict-export` 同样不经过
`pipeline`，直接调用 `dictionary_manager` 模块的对应函数，与
`/api/dictionary/*` 系列路由背后是同一批函数。`settings-get`/
`settings-set` 同理，直接调用 `app_settings.load_settings()`/
`save_settings()`，与 `/api/settings` 路由背后是同一批函数。

`dialogue-batch` 里的 TTS 跟读框是"半个例外"：`pipeline.process_dialogue_batch()`
本身完全不知道 TTS 的存在，`commandline.py` 在调用它之前，先对每个带
`tts` 信息的框调用 `tts_processor.synthesize_and_align()` 合成 + 对齐、
回填 `audio_path`/`lab_path`，这一步和网页版 `run_dialogue_batch_job()`
里预处理 TTS 跟读框是同一个函数、同一段逻辑，只是命令行版没有"先点
预览再提交复用"这个交互步骤。回填完之后，再走一遍和音频跟读框完全
一样的 `pipeline.process_dialogue_batch()`。

---

## 5. 常见问题

**Q: 打包后 `启动器.exe cmd ...` 提示"找不到命令行功能依赖的 Python 运行时"？**
A: 说明 `runtime\mfa_env\python.exe` 不存在。命令行模式固定使用 `mfa_env`
（和界面模式里的主服务 `app.py` 是同一个环境），需要先跑一遍 `pack_runtime.bat`
把开发用的 `.mfa_env` 打包进 `runtime\mfa_env\`，再重新发布。

**Q: 双击 `启动器.exe` 会不会也进命令行模式？**
A: 不会。判断依据是 `argv[1] == "cmd"`——双击启动没有命令行参数，
自然走正常的界面模式（托盘 + 原生窗口 + 三个后端服务）。

**Q: 命令行模式下会不会也拉起 qwen3_server.py / nemo_server.py？**
A: 不会，命令行模式全程只有 `app.py`（`mfa_env`）一个进程，需要哪个微服务
都得用户自己先启动好（正常双击 `启动器.exe` 的界面模式会自动拉起）。两种
情况区别一下：
  - `--aligner-backend qwen3_asr`/`qwen3_aligner`/`nemo_aligner`（`mfa-only`/
    `full`/`dialogue-batch` 的可选项）：是否需要对应微服务已经在运行、还是
    按需临时加载模型，以 `pipeline.py`/`alt_aligners.py` 的实现为准。
  - `asr-subtitle`：**固定**依赖 `qwen3_server.py`（无论传不传
    `--aligner-backend`，这个子命令本身就是靠 HTTP 调用它做识别），没有
    "本地直接跑不需要额外微服务"这种选项，服务未启动时会在开始识别前就
    检测到并报错退出。

**Q: `--json` 输出的结果里，字段一定是 `success`/`error` 吗？**
A: 是，全部子命令统一走这两个字段判断成功/失败，额外字段因操作而异
（比如 `mfa-only` 有 `lab_path`，`f0-only` 有 `frames`/`sample_rate`，
`project-only`/`full` 有 `project_path`/`segments`，`asr-subtitle` 有
`count`/`entries`），传了 `-o/--output` 的话还会多一个 `final_output_path`。

**Q: 能不能同时传 `-t/--text` 和 `-T/--text-file`？**
A: 能传，但 `--text` 优先，`--text-file` 会被忽略（`commandline.py` 里
`_read_text()` 的判断顺序）。
