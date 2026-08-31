# -*- coding: utf-8 -*-
"""
应用级设置管理：模型自动更新开关（HF_HUB_OFFLINE）、镜像站下载配置（HF_ENDPOINT），
以及 alt_aligners.py 里一批可调的对齐后处理调优参数。

设计说明
────────
【HF_HUB_OFFLINE / HF_ENDPOINT】
这两个环境变量分别由三个各自独立的进程消费：
  - app.py（经由其在模块顶层 import 的 alt_aligners.py——现在 Qwen3-ASR /
    Qwen3-FA 也在这个进程内本地加载模型，同样消费这两个变量）
  - whisperx_server.py（独立 venv 子服务，端口 5854，WhisperX）
  - nemo_server.py（独立 venv 子服务，端口 5852，NeMo-FA）
  - qwen3tts_server.py（独立 venv 子服务，端口 5853，Qwen3-TTS 跟读语音合成）

四者都会在各自最早的时机（import huggingface_hub / transformers /
whisperx / nemo / qwen_tts 之前）调用本模块的 apply_env_from_settings()，
从同一份 JSON 配置文件读取设置并写入 os.environ。这样设置页面只需要写
一次文件，各进程各自重启后即可生效——环境变量只在"进程启动时"读取
一次，所以修改设置后必须重启对应进程才能让新配置真正生效。

【2026-08 起】WhisperX / NeMo / Qwen3-TTS 三个独立子服务仍可以通过各自
的 /restart 路由自我重启来应用新设置（见 app.py 里的
_restart_microservice()）；但 Qwen3-ASR / Qwen3-FA 已迁入主进程
（app.py）内本地加载，主进程本身没有对应的自重启机制（它是
launcher.py 拉起的顶层进程），因此保存新的模型下载设置后，Qwen3-ASR /
Qwen3-FA 这两项要等下一次手动重启整个程序才会生效——这一点在设置页面
的提示文案里需要向用户说清楚（与 WhisperX / NeMo / Qwen3-TTS 三者"保存
后立即自动生效"的体验不同）。

【对齐调优参数】
与上面两个环境变量不同，以下这批参数只被 alt_aligners.py 消费，而
alt_aligners.py 只在主进程（app.py）里被 import——不涉及 Qwen3 / NeMo
两个独立子服务，因此**不需要重启任何进程**：alt_aligners.py 通过本模块的
get_alignment_tuning() 在每次调用时实时读取最新值（内部仍有轻量缓存，
文件 mtime 变化时自动刷新），保存设置后下一次对齐任务即可生效。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = PROJECT_DIR / "settings" / "app_settings.json"

_lock = threading.RLock()

DEFAULT_SETTINGS: Dict[str, object] = {
    # False → HF_HUB_OFFLINE=1（允许 huggingface_hub 联网检查/下载模型）
    # True  → HF_HUB_OFFLINE=0（默认禁用自动联网更新，行为与改造前一致，最省心）
    "auto_update_models": False,
    "use_mirror": False,
    "mirror_url": "https://hf-mirror.com/",

    # 【2026-08 起，历史遗留字段，正常发布环境下不再有实际效果】
    # 早期版本：True → 尝试隐藏 app.py / whisperx_server.py / qwen3tts_server.py /
    # nemo_server.py 四个进程各自所在的命令提示符（终端）窗口；进程本身
    # 继续正常运行，只是窗口不可见。
    #
    # 现状：launcher.py 正常启动这四个进程时改用了 CREATE_NO_WINDOW，从
    # 系统层面直接不分配任何控制台窗口——不管这个开关是 True 还是 False，
    # 任务栏上都不会出现任何相关窗口/图标，日志改为写入 logs/<name>.log
    # 文件查看。原因：CREATE_NEW_CONSOLE + ShowWindow(SW_HIDE) 这一套在
    # 把"默认终端应用"设为 Windows Terminal 的 Windows 11 系统上（22H2
    # 起的系统默认值）会被 Windows Terminal 接管出一个独立顶层窗口，隐藏
    # 底层句柄对任务栏毫无效果，所以改为从源头不创建窗口。
    #
    # 保留这个字段只是为了兼容旧版本写过的设置文件（避免加载报错）；仅在
    # 有人直接用 `python app.py` / `python whisperx_server.py` 等命令在真实
    # 终端里手动调试时，apply_console_visibility() 仍会按这个值隐藏/显示
    # 那个真实终端窗口——但这不是 launcher.py 正常拉起服务的路径。设置
    # 页面的这个开关目前保留但已在提示文案里注明"当前版本已默认不显示，
    # 无需再手动隐藏"。
    "hide_console_window": False,

    # True  → 下次完整启动本应用（即重新打开 exe 启动器）时，不再自动拉起
    #         whisperx_server.py / nemo_server.py 对应的那个微服务进程。
    # False → 下次启动时正常拉起（默认）。
    # 与 hide_console_window 不同，这两项不会触发/也不需要触发任何"立即
    # 生效"的动作——它们只在下一次完整启动应用（即启动器重新拉起三个子
    # 进程）时被读取一次，对当前已经在运行的进程没有任何影响，保存设置
    # 本身也不会关闭正在运行的服务。具体的"跳过启动"逻辑在启动器
    # (launcher.py) 里实现：它在拉起子进程之前读取这两个字段。
    #
    # 【2026-08 起】Qwen3-ASR / Qwen3-ForcedAligner 已迁入 app.py 主进程
    # 内本地加载，不再是可以单独跳过启动的独立微服务，因此
    # skip_start_qwen3_server 已重命名为 skip_start_whisperx_server（原
    # 字段名保留仅为兼容旧配置文件读取，见 save_settings() 里的迁移逻辑，
    # 不再写回旧字段名）。
    "skip_start_whisperx_server": False,
    "skip_start_nemo_server": False,
    "skip_start_qwen3tts_server": False,

    # ── Qwen3-TTS（TTS跟读独立引擎，qwen3tts_server.py，端口 5853）────────
    # 模型规模：1.7B 效果最好但显存需求更高；0.6B 更省显存/更快。
    # VoiceDesign（仅文本描述）目前只有 1.7B 权重，选择 0.6B 时该模式会
    # 自动回退到 1.7B（见 qwen3tts_server.py MODEL_IDS 说明）。
    "qwen3_tts_model_size": "1.7B",
    # Voice Clone（Base 模型）默认是否启用"仅 x-vector"模式：
    #   True  → 只提取参考音频的说话人特征（x-vector），不需要参考文本，
    #           更省事但克隆质量可能下降；
    #   False → 同时使用参考文本做更精确的克隆（默认，质量更好）。
    # 前端"语音预设管理"里针对 Voice Clone 的"Use x-vector only"开关，
    # 新建预设时以此为默认勾选状态。
    "qwen3_tts_x_vector_only_default": False,

    # ── alt_aligners.py 对齐调优参数（默认值与原硬编码常量保持一致）────────
    # Qwen3-ForcedAligner / Qwen3-ASR 全局事后偏移校正（秒）。
    "qwen3_fa_onset_delay_sec": 0.06,
    "qwen3_asr_onset_delay_sec": 0.06,
    # 偏移校正后允许的最短音节时长（秒），避免被压成 0 或负数。
    "qwen3_fa_min_syl_dur_sec": 0.02,
    "qwen3_asr_min_syl_dur_sec": 0.02,
    # CTC 短语边界拉伸修复：各类 token 的时长上限 / 最小可插入 SP 时长（秒）。
    "ctc_max_cjk_char_sec": 0.50,
    "ctc_max_cjk_particle_sec": 0.35,
    "ctc_max_en_word_sec": 1.20,
    "ctc_min_sp_sec": 0.15,

    # Qwen3-ForcedAligner 长音频分段对齐（详见 alt_aligners.py 里
    # Qwen3ForcedAligner._align_chunked() / _plan_sentence_aligned_chunks()
    # 的说明）。
    #
    # 【总开关】是否启用"按句子分段对齐"这一整套流程。
    # False → 禁用（默认）：Qwen3ForcedAligner.align() 跳过
    #         _align_chunked() 规划分段这一步，始终走 _align_single_chunk()
    #         整段单次对齐——不管音频多长、参考文本能不能按标点切出多句，
    #         都不做静音感知分段。行为与改造前（v3 分段逻辑引入之前）
    #         完全一致。
    # True  → 启用：按 _align_chunked() 里的规则，先按参考文本句末标点
    #         规划分段边界，再逐段独立对齐（详见下面两项 min/max 的说明）。
    #
    # 该开关与下面【WhisperX 粗测预处理】(qwen3_fa_use_whisperx_prepass)
    # 是父子关系：粗测预处理只在"按句子分段对齐"这一整套流程内部生效
    # （用于规划分段边界），一旦本开关被设为 False，粗测预处理开关会被
    # save_settings() 强制一并置为 False，避免出现"分段对齐已禁用，但
    # 粗测预处理仍显示已开启"这种无意义的状态。
    "qwen3_fa_enable_sentence_chunking": False,
    #
    # 【v3】此前的 qwen3_fa_chunk_threshold_sec（音频超过多少秒才分段）和
    # qwen3_fa_chunk_target_sec（每段目标多长）已废弃移除——这两个参数只按
    # 音频总时长的比例猜句子边界，猜错了就会把切点定在句子中间。现在完全
    # 改为按参考文本自身的句末标点（。！？.!?…；\n）分段：每个完整句子
    # 默认就是独立的一段，不再需要"音频够长才分段"这个前置条件，也不再
    # 需要一个目标时长把多句"凑"成一段。
    #
    # 下面两项只用来处理分句之后的两种边缘情况，且只在
    # qwen3_fa_enable_sentence_chunking 为 True 时才会生效：
    # 单句时长下限（秒）：短于此值的句子（如单字/叹词句"嗯。""啊！"）会
    # 并入相邻句子，避免生成过短、难以稳定对齐的独立音频片段。
    "qwen3_fa_min_sentence_chunk_sec": 3.0,
    # 单句时长上限（秒）：长于此值的单句会先按句内逗号/顿号再切一刀，
    # 避免整句被硬塞进一个过长的独立片段。
    "qwen3_fa_max_sentence_chunk_sec": 20.0,

    # ── Qwen3-FA 长音频分段：WhisperX 粗测时间戳预处理（可选）──────────────
    # 仅在 qwen3_fa_enable_sentence_chunking 为 True（即"按句子分段对齐"
    # 总开关已启用）时才有意义——分段对齐本身被禁用时，不存在"规划分段
    # 边界"这一步，本开关会被 save_settings() 强制一并置为 False。
    #
    # True  → 在 Qwen3-ForcedAligner 做长音频分段对齐之前，先用 WhisperX
    #         对同一段音频做一次轻量 ASR 转录（只转录，不做 wav2vec2 强制
    #         对齐），借用 Whisper 自身 VAD 分段给出的真实语音起止时间戳
    #         规划分段边界，替代原来"假设语速均匀、按参考文本字符数占比
    #         反推时间"的估算方案——演唱/拖腔/语速不均的素材上，字符占比
    #         估算的系统性误差可达 1~2 秒，往往导致喂给 Qwen3-FA 的每一段
    #         物理边界本身就没卡准，进而触发大量"自愈修复/均匀分配"退化
    #         兜底。需要预先安装 whisperx（pip install whisperx）；未安装
    #         或识别失败时自动回退到原方案，不影响任务本身是否成功。
    # False → 保持原来的字符比例估算方案（默认，不引入 WhisperX 依赖）。
    "qwen3_fa_use_whisperx_prepass": False,
    # 上面这次"粗测"专用的 Whisper 模型档位——只影响这一步的速度/准确度，
    # 与"WhisperX"作为独立对齐后端（backend="whisperx"）时使用的模型档位
    # 互不影响，是两次独立的模型加载/调用。可选值见 alt_aligners.py 里
    # WhisperXAligner.SUPPORTED_MODELS（"large-v3" / "large-v3-turbo" /
    # "large-v2" / "medium" / "small" / "base" / "tiny"）；这里只是定位
    # 分段边界，不追求最高识别精度，可以选比主对齐更小/更快的档位。
    "qwen3_fa_whisperx_prepass_model": "large-v3",

    # ── WhisperX ASR 转录 batch_size（独立对齐后端 + 上面 Qwen3-FA 粗测
    # 预处理共用同一个设置）─────────────────────────────────────────────
    # 默认 16（与 whisperx 官方默认一致）。低显存显卡（例如 6GB 的老款
    # 矿卡）在 large-v3 + batch_size=16 下容易在 ASR 转录阶段直接 CUDA
    # 显存不足；alt_aligners.py 已经内置了显存不足时自动腰斩 batch_size
    # 重试的逻辑（见 WhisperXAligner._transcribe_with_oom_retry），但如果
    # 已知自己的显卡显存有限，也可以在这里直接调低这个值，跳过重试直接
    # 一次成功、减少无谓的重试耗时；反之显存充裕的显卡可以调大以提速。
    "whisperx_batch_size": 16,

    # ── Qwen3-ASR / Qwen3-ForcedAligner / NeMo Forced Aligner batch_size ──
    # 三者底层都不像 WhisperX 那样天然支持"多条音频一批推理"（本项目
    # 每次对齐任务始终只送 1 条音频/1 个分段），因此这个设置的实际含义
    # 与 whisperx_batch_size 不完全一样，按后端拆成两种用法：
    #
    #   - Qwen3-ASR：直接透传给 qwen_asr.Qwen3ASRModel.from_pretrained(...)
    #     的 max_inference_batch_size（官方参数，限制模型内部单次推理的
    #     最大批量，-1 为不限制）。原 qwen3_server.py 对 GPU 硬编码为 8，
    #     这里把它变成可调项；值越小，显存占用峰值越低。
    #   - Qwen3-ForcedAligner / NeMo Forced Aligner：两者服务端调用本身
    #     就是单音频单次前向，没有真正的"批"概念可调；这个值改为用作
    #     "显存不足时自动降级重试"的起始参考批大小——命中 CUDA OOM 时，
    #     会从这个值开始按腰斩策略重试（间接影响分块/降精度等自愈路径的
    #     起始激进程度），不会在正常（不 OOM）情况下影响任何行为。
    #
    # 默认 8，低显存显卡可调小；三个后端共用同一个设置项，与
    # whisperx_batch_size 是各自独立的两个调优参数，互不影响。
    "qwen3_batch_size": 8,

    # ── tts_processor.py 逐句合成分段长度（字符数）───────────────────────
    # 详见 tts_processor.py split_sentences() / _split_long_line() 顶部
    # 说明：单行文本超过 tts_max_segment_len 才会被二次切割，切割点优先
    # 落在 [tts_min_segment_len, tts_max_segment_len] 区间内最靠近上限的
    # 句末标点（找不到则退化为逗号类标点）。默认值与原硬编码常量一致
    # （250 / 500）。这两项只影响"TTS 跟读"逐句合成的分段粒度，不涉及
    # Qwen3 / NeMo 两个独立子服务，保存后无需重启任何进程。
    "tts_min_segment_len": 250,
    "tts_max_segment_len": 500,

    # 每多少个换行才切一段（默认 1，即每遇到一个换行就分段，与改造前行为
    # 一致）。例如设为 2 表示每两个换行才断开一次分段，未达到该数量的换行
    # 会被当作段内换行保留，不再单独触发切分。仅在 tts_disable_newline_split
    # 为 False 时生效。
    "tts_newline_split_every_n": 1,

    # True  → 完全禁用"按换行分段"这一步，整段输入文本先被当成一整段
    #         文本处理，是否切割、在哪切割完全交给下面的长度区间二次
    #         切割规则（tts_min_segment_len / tts_max_segment_len）决定。
    # False → 保持按换行分段（默认，行为与改造前一致，换行切分粒度由
    #         tts_newline_split_every_n 控制）。
    "tts_disable_newline_split": False,

    # True  → 完全禁用"单段过长再二次切割"这一步：不管文本多长，都不会
    #         再按 tts_min_segment_len / tts_max_segment_len 区间寻找标点
    #         切割点，每个分段保持原样（分段仍然可能来自换行切分，除非
    #         上面 tts_disable_newline_split 也同时打开）。
    # False → 保持二次切割（默认，行为与改造前一致）。
    "tts_disable_segment_len_split": False,

    # ── 序列时间表调试文件输出（对话文本框批量处理 + TTS跟读共用总开关）──
    # True  → 每次「对话文本框批量处理」成功生成工程文件后，额外在工作
    #         目录下写一份 <project_title>.dtslt（JSON，UTF-8）：记录
    #         每个对话框被加入合并时间轴的起始/结束时间（秒）、原始
    #         台词文本、以及该对话框的 LAB 音标条目（起止时间 + 音标 +
    #         devoiced_phoneme，音标已经是 phoneme_mode 转换后的最终
    #         结果，与写入工程文件的音符音素完全一致）。
    #         同时，「TTS跟读」每次 align_segments() 成功对齐一批分句
    #         后，额外写一份 <stem>.ttslt（JSON，UTF-8）：记录每句在
    #         合并音频中的起始/结束时间（秒）、原始文本、以及该句的
    #         LAB 音标条目（TTS 流程不经过 phoneme_mode 转换，音标即
    #         Qwen3-ForcedAligner 对齐输出的原始音标）。
    # False → 不生成这两类调试文件（默认，与改造前行为一致，不产生
    #         额外磁盘 I/O）。
    # 两个功能共用这一个开关，不单独区分；不影响进度接口回填的
    # sequence_timeline 内存字段（那个不受此开关控制，见
    # tsubaki_processor.build_multitrack_project 说明）。保存后下一次
    # 任务立即生效，无需重启任何进程。
    "output_timeline_files": False,

    # ── subtitle_import.py 字幕跟读：跳过分割音频 ──────────────────────────
    # 仅影响"字幕跟读"（上传整段音频 + SRT/LRC，按字幕时间轴切分音频固定
    # 交给 Qwen3-ForcedAligner 逐段对齐）这一个功能，不影响其它任何对齐
    # 入口（音频跟读/TTS跟读/字幕识别等）。
    #
    # 默认 1：每一条字幕时间轴单独切一段音频、单独送 Qwen3-FA 对齐一次，
    # 与改造前行为完全一致。
    #
    # 设为 N（> 1）：连续且中间没有静音间隙的相邻字幕，每凑够 N 条才切
    # 一次音频——例如设为 4，第 1~4 条字幕会被合并成一段连续音频一起送
    # Qwen3-FA 对齐一次，在第 5 条字幕开头才重新切一刀开始下一组。字幕
    # 之间原本存在的静音停顿不受影响，仍然会被单独抽出来保留为 SIL、
    # 不参与合并也不参与对齐；只有"背靠背挨在一起、中间没有停顿"的字幕
    # 才会被合并送同一次对齐调用。合并对齐只保证整段音频的 LAB 时间轴
    # 准确，不再保留每条原始字幕各自的独立边界。
    "subtitle_import_skip_split_every_n": 1,
}

# 对齐调优参数的合法取值范围（秒），用于 save_settings() 里的边界钳制。
# 上限给得比较宽松，只用来挡住明显的输入错误（比如误输成毫秒级数字 60）。
_TUNING_RANGES: Dict[str, tuple] = {
    "qwen3_fa_onset_delay_sec": (0.0, 2.0),
    "qwen3_asr_onset_delay_sec": (0.0, 2.0),
    "qwen3_fa_min_syl_dur_sec": (0.0, 1.0),
    "qwen3_asr_min_syl_dur_sec": (0.0, 1.0),
    "ctc_max_cjk_char_sec": (0.05, 5.0),
    "ctc_max_cjk_particle_sec": (0.05, 5.0),
    "ctc_max_en_word_sec": (0.05, 5.0),
    "ctc_min_sp_sec": (0.0, 2.0),
    # 单句时长下限不允许为 0/负数（否则任意短句都会立刻单独成段）；
    # 上限也需要明显大于下限，避免两者钳制后颠倒。
    "qwen3_fa_min_sentence_chunk_sec": (0.1, 600.0),
    "qwen3_fa_max_sentence_chunk_sec": (1.0, 600.0),
}

# alt_aligners.get_alignment_tuning() 使用的轻量缓存：避免每次对齐调用都
# 重新读盘/解析 JSON。用文件 mtime 判断是否需要刷新，保存设置后下一次
# 对齐任务立即生效，无需重启任何进程。
_tuning_cache_lock = threading.RLock()
_tuning_cache: Dict[str, float] = {}
_tuning_cache_mtime: float = -1.0


def load_settings() -> Dict[str, object]:
    with _lock:
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                merged = dict(DEFAULT_SETTINGS)
                if isinstance(data, dict):
                    # 【2026-08 迁移】skip_start_qwen3_server 已重命名为
                    # skip_start_whisperx_server（Qwen3-ASR/FA 迁入主进程
                    # 后不再是可跳过启动的独立微服务，WhisperX 反过来变成
                    # 了新的独立微服务）。旧配置文件里如果还带着旧字段名，
                    # 这里读出它的值当作新字段的初始值，避免用户之前设置
                    # 的"跳过启动"选择在这次架构调整后被静默丢弃——旧字段
                    # 不在 DEFAULT_SETTINGS 里，下面 update() 时会被自动
                    # 过滤掉，不会残留写回文件。
                    if "skip_start_qwen3_server" in data and "skip_start_whisperx_server" not in data:
                        merged["skip_start_whisperx_server"] = bool(data["skip_start_qwen3_server"])
                    merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
                return merged
            except Exception as e:
                logger.error("读取设置文件失败（%s），使用默认设置: %s", SETTINGS_PATH, e)
                return dict(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)


def save_settings(new_settings: Dict[str, object]) -> Dict[str, object]:
    with _lock:
        current = load_settings()
        current.update({k: v for k, v in (new_settings or {}).items() if k in DEFAULT_SETTINGS})

        # 基本校验，避免脏数据写入
        current["auto_update_models"] = bool(current.get("auto_update_models"))
        current["use_mirror"] = bool(current.get("use_mirror"))
        mirror_url = str(current.get("mirror_url") or "").strip()
        current["mirror_url"] = mirror_url or DEFAULT_SETTINGS["mirror_url"]
        current["hide_console_window"] = bool(current.get("hide_console_window"))
        current["skip_start_whisperx_server"] = bool(current.get("skip_start_whisperx_server"))
        current["skip_start_nemo_server"] = bool(current.get("skip_start_nemo_server"))
        current["skip_start_qwen3tts_server"] = bool(current.get("skip_start_qwen3tts_server"))

        # Qwen3-TTS 模型规模：只允许 "1.7B" / "0.6B"，非法值回退默认值。
        tts_size = str(current.get("qwen3_tts_model_size") or "").strip()
        current["qwen3_tts_model_size"] = (
            tts_size if tts_size in ("1.7B", "0.6B") else DEFAULT_SETTINGS["qwen3_tts_model_size"]
        )
        current["qwen3_tts_x_vector_only_default"] = bool(current.get("qwen3_tts_x_vector_only_default"))

        current["output_timeline_files"] = bool(current.get("output_timeline_files"))

        # Qwen3-FA「按句子分段对齐」总开关，以及与其构成父子关系的
        # WhisperX 粗测预处理：bool 开关 + 模型档位字符串（非法/空值回退
        # 为默认档位，逻辑与上面 mirror_url 的兜底一致）。
        current["qwen3_fa_enable_sentence_chunking"] = bool(current.get("qwen3_fa_enable_sentence_chunking"))
        current["qwen3_fa_use_whisperx_prepass"] = bool(current.get("qwen3_fa_use_whisperx_prepass"))
        # 联动：总开关（按句子分段对齐）关闭时，粗测预处理这个子开关
        # 强制一并关闭——不管前端本次提交了什么值，都不允许"分段对齐
        # 已禁用，但粗测预处理仍显示已开启"这种矛盾状态被写入磁盘。
        # 前端已经做了同步的 UI 隐藏 + 置空处理，这里是服务端侧的最终
        # 兜底，防止绕过前端直接调用 /api/settings 写入脏数据。
        if not current["qwen3_fa_enable_sentence_chunking"]:
            current["qwen3_fa_use_whisperx_prepass"] = False
        prepass_model = str(current.get("qwen3_fa_whisperx_prepass_model") or "").strip()
        current["qwen3_fa_whisperx_prepass_model"] = (
            prepass_model or DEFAULT_SETTINGS["qwen3_fa_whisperx_prepass_model"]
        )

        # WhisperX batch_size：转 int 并钳制到 [1, 128]，非法/缺失值回退
        # 为默认值 16，避免 0/负数/非数字导致 transcribe() 抛出难以理解
        # 的错误。
        try:
            wx_bs = int(current.get("whisperx_batch_size", DEFAULT_SETTINGS["whisperx_batch_size"]))
        except (TypeError, ValueError):
            wx_bs = int(DEFAULT_SETTINGS["whisperx_batch_size"])
        current["whisperx_batch_size"] = min(max(wx_bs, 1), 128)

        # Qwen3-ASR / Qwen3-ForcedAligner / NeMo Forced Aligner batch_size：
        # 同样转 int 并钳制到 [1, 128]，非法/缺失值回退为默认值 8，与
        # whisperx_batch_size 的校验逻辑保持一致。
        try:
            q3_bs = int(current.get("qwen3_batch_size", DEFAULT_SETTINGS["qwen3_batch_size"]))
        except (TypeError, ValueError):
            q3_bs = int(DEFAULT_SETTINGS["qwen3_batch_size"])
        current["qwen3_batch_size"] = min(max(q3_bs, 1), 128)

        # TTS 逐句合成分段长度（字符数）：转 int 并各自钳制到 [1, 5000]，
        # 非法/缺失值回退为默认值；钳制后若 min > max，交换两者，避免
        # split_sentences() 内部区间倒置导致行为异常（例如永远找不到切
        # 割点、二次切割死循环）。上限 5000 只是防呆，正常使用场景里
        # 段落长度远小于此值。
        try:
            tts_min_len = int(current.get("tts_min_segment_len", DEFAULT_SETTINGS["tts_min_segment_len"]))
        except (TypeError, ValueError):
            tts_min_len = int(DEFAULT_SETTINGS["tts_min_segment_len"])
        try:
            tts_max_len = int(current.get("tts_max_segment_len", DEFAULT_SETTINGS["tts_max_segment_len"]))
        except (TypeError, ValueError):
            tts_max_len = int(DEFAULT_SETTINGS["tts_max_segment_len"])
        tts_min_len = min(max(tts_min_len, 1), 5000)
        tts_max_len = min(max(tts_max_len, 1), 5000)
        if tts_min_len > tts_max_len:
            tts_min_len, tts_max_len = tts_max_len, tts_min_len
        current["tts_min_segment_len"] = tts_min_len
        current["tts_max_segment_len"] = tts_max_len

        # 每多少个换行才切一段：转 int 并钳制到 [1, 100]，非法/缺失值回退
        # 为默认值 1。上限 100 只是防呆（正常场景很少需要连续攒几十个换行
        # 才分段）。
        try:
            newline_every_n = int(current.get(
                "tts_newline_split_every_n", DEFAULT_SETTINGS["tts_newline_split_every_n"]
            ))
        except (TypeError, ValueError):
            newline_every_n = int(DEFAULT_SETTINGS["tts_newline_split_every_n"])
        current["tts_newline_split_every_n"] = min(max(newline_every_n, 1), 100)

        # 两个新增开关：与其它布尔项一样做基本校验。
        current["tts_disable_newline_split"] = bool(current.get("tts_disable_newline_split"))
        current["tts_disable_segment_len_split"] = bool(current.get("tts_disable_segment_len_split"))

        # 字幕跟读"每多少个时间轴跳过分割音频"：转 int 并钳制到 [1, 50]，
        # 非法/缺失值回退为默认值 1。上限 50 只是防呆（正常场景很少需要
        # 一次合并几十条字幕对齐，过大反而会让单次 Qwen3-FA 调用的音频
        # 过长，增大对齐出错概率）。
        try:
            skip_split_n = int(current.get(
                "subtitle_import_skip_split_every_n",
                DEFAULT_SETTINGS["subtitle_import_skip_split_every_n"],
            ))
        except (TypeError, ValueError):
            skip_split_n = int(DEFAULT_SETTINGS["subtitle_import_skip_split_every_n"])
        current["subtitle_import_skip_split_every_n"] = min(max(skip_split_n, 1), 50)

        # 对齐调优参数：转 float 并钳制到 _TUNING_RANGES 定义的合法区间，
        # 非法/缺失/无法解析的值一律回退为默认值，避免脏数据写入导致
        # alt_aligners.py 后续计算出负数时长等异常。
        for key, (lo, hi) in _TUNING_RANGES.items():
            raw = current.get(key, DEFAULT_SETTINGS[key])
            try:
                val = float(raw)
                if val != val:  # NaN
                    raise ValueError("NaN")
            except (TypeError, ValueError):
                logger.warning("设置项 %s 的值 %r 无效，回退为默认值 %s", key, raw, DEFAULT_SETTINGS[key])
                val = float(DEFAULT_SETTINGS[key])
            current[key] = min(max(val, lo), hi)

        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(SETTINGS_PATH)

        # 对齐调优参数已随本次保存写入磁盘；让 get_alignment_tuning() 的
        # 缓存立即失效，这样即使当前请求处理进程与下一次对齐任务用的是
        # 同一进程内的缓存副本，也不会读到过期值（正常情况下 mtime 检测
        # 已经足够，这里是双保险，成本很低）。
        global _tuning_cache_mtime
        with _tuning_cache_lock:
            _tuning_cache_mtime = -1.0

        return current


def apply_env_from_settings() -> Dict[str, object]:
    """
    在进程启动早期调用：把设置文件内容映射为环境变量。

    必须在 import huggingface_hub / transformers / qwen_asr / nemo_toolkit
    之前调用，这些库只在 import 时读取一次 HF_HUB_OFFLINE / HF_ENDPOINT，
    之后修改 os.environ 不会再生效（需要重启进程）。
    """
    settings = load_settings()

    # 逻辑反转：当前端开关打开（True）代表用户想要“禁用更新”，所以设置为 "1"
    os.environ["HF_HUB_OFFLINE"] = "1" if settings.get("auto_update_models") else "0"

    if settings.get("use_mirror") and settings.get("mirror_url"):
        os.environ["HF_ENDPOINT"] = str(settings["mirror_url"]).rstrip("/") + "/"
    else:
        os.environ.pop("HF_ENDPOINT", None)

    logger.info(
        "已应用模型下载设置: HF_HUB_OFFLINE=%s, HF_ENDPOINT=%s",
        os.environ.get("HF_HUB_OFFLINE"),
        os.environ.get("HF_ENDPOINT", "(未设置，使用官方 huggingface.co)"),
    )
    return settings


def apply_console_visibility() -> bool:
    """
    显示或隐藏当前进程所在的命令提示符（控制台）窗口，取决于设置文件里
    hide_console_window 的当前值。

    【2026-08 起，重要】launcher.py 正常拉起 app.py / whisperx_server.py /
    qwen3tts_server.py / nemo_server.py 这四个服务时，已经改用
    CREATE_NO_WINDOW，从系统层面直接不为子进程分配任何控制台窗口——这种
    情况下本函数里的 GetConsoleWindow() 会返回空句柄，函数直接走
    `if not hwnd: return False` 分支提前返回，是预期中的"无操作"，不会
    报错也不会有任何副作用。本函数保留下来，只是为了兼容"有人直接用
    `python app.py` 之类命令、在一个真实的命令提示符/PowerShell 窗口里
    手动调试"这种场景——这种场景下确实存在一个真实控制台窗口，下面这套
    ShowWindow() 逻辑仍然会按预期隐藏/显示它。

    （以下为函数本身的实现说明，在有真实控制台窗口时仍然适用）

    与 apply_env_from_settings() 不同，这里不是"只能在启动早期调用一次"：
    ShowWindow() 可以在进程运行期间随时调用，每次调用都会把窗口设为当前
    设置要求的显示/隐藏状态，是完全可逆、可重复调用的操作——不会杀掉进程，
    也不影响 Flask 服务本身，只是这个窗口本身在桌面/任务栏上看不看得见。

    这里用的是 SW_HIDE 而不是 SW_MINIMIZE：两者都会让窗口从桌面上消失，
    但 SW_MINIMIZE 会在任务栏留下一个图标，SW_HIDE 会把任务栏图标也一并
    移除——用户要的是"任务栏上完全看不到这个命令提示符"，所以选 SW_HIDE。
    对应地，恢复显示用 SW_SHOW，窗口和任务栏图标会一起回来。

    这套"隐藏窗口"的思路本身，在把"默认终端应用"设为 Windows Terminal 的
    Windows 11 系统上（22H2 起的系统默认值）存在已知局限：如果控制台是被
    CREATE_NEW_CONSOLE 创建、又被 Windows Terminal 接管包了一层独立顶层
    窗口，GetConsoleWindow() 拿到的是接管前的底层句柄，隐藏它对任务栏上
    真正显示的 Windows Terminal 窗口没有效果。这正是 launcher.py 改用
    CREATE_NO_WINDOW（见上面的说明）而不是继续依赖本函数的原因。

    调用时机（均为兼容手动调试场景保留，正常发布环境下是无操作）：
      - app.py：在 main() 启动时调用一次；另外在 /api/settings 保存设置
        时也会调用一次。
      - whisperx_server.py / qwen3tts_server.py / nemo_server.py：在模块
        导入阶段、紧跟 apply_env_from_settings() 之后调用一次。

    仅在 Windows 上有效（用到 kernel32 / user32 的 GetConsoleWindow /
    ShowWindow）；非 Windows 平台直接跳过并返回 False。

    Returns
    -------
    bool：本次是否成功找到控制台窗口并设置了显示状态。False 常见于：非
    Windows 平台、子进程本身就是用 CREATE_NO_WINDOW 拉起的（launcher.py
    正常发布环境下的默认情况）、或找不到控制台窗口（例如从某些 IDE 的
    集成终端启动、或 stdout 被完全重定向导致没有关联的控制台）——以上
    几种情况都不算错误。
    """
    if os.name != "nt":
        return False
    try:
        settings = load_settings()
        hide = bool(settings.get("hide_console_window"))

        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return False

        SW_HIDE = 0
        SW_SHOW = 5
        ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE if hide else SW_SHOW)
        logger.info("已将控制台窗口设置为: %s", "隐藏" if hide else "显示")
        return True
    except Exception as e:
        logger.warning("设置控制台窗口显示状态失败（不影响服务本身运行）: %s", e)
        return False


def get_alignment_tuning() -> Dict[str, float]:
    """
    供 alt_aligners.py 调用：实时读取一批对齐后处理调优参数（均为秒）。

    与 apply_env_from_settings() 不同，这里刻意不缓存到 os.environ、也不要求
    在进程启动早期调用——alt_aligners.py 只在主进程内运行，每次对齐任务
    调用本函数即可拿到最新值，保存设置后无需重启任何进程。

    内部用文件 mtime 做了一层缓存：同一个 app_settings.json 没有变化时，
    重复调用不会重新读盘/解析 JSON（对齐流程里同一次任务可能会多次用到
    这些参数）。文件不存在（尚未保存过任何设置）时返回内置默认值。

    Returns
    -------
    Dict[str, float]，键固定为 _TUNING_RANGES 中的 10 个参数名。
    """
    global _tuning_cache, _tuning_cache_mtime

    try:
        mtime = SETTINGS_PATH.stat().st_mtime if SETTINGS_PATH.exists() else 0.0
    except OSError:
        mtime = 0.0

    with _tuning_cache_lock:
        if _tuning_cache and mtime == _tuning_cache_mtime:
            return dict(_tuning_cache)

    settings = load_settings()
    tuning: Dict[str, float] = {}
    for key in _TUNING_RANGES:
        try:
            tuning[key] = float(settings.get(key, DEFAULT_SETTINGS[key]))
        except (TypeError, ValueError):
            tuning[key] = float(DEFAULT_SETTINGS[key])

    with _tuning_cache_lock:
        _tuning_cache = dict(tuning)
        _tuning_cache_mtime = mtime

    return tuning


def get_tts_segment_len() -> Dict[str, int]:
    """
    供 tts_processor.py 调用：实时读取逐句合成分段长度设置
    （tts_min_segment_len / tts_max_segment_len，均为字符数）。

    与 get_alignment_tuning() 同样的"实时生效、无需重启"模型——不使用
    单独的 mtime 缓存（tts_processor 调用频率远低于 alt_aligners 的
    对齐热路径），每次直接调用 load_settings()，成本可忽略。

    Returns
    -------
    Dict[str, int]，键为 "min_len" / "max_len"，保证 min_len <= max_len
    （save_settings() 已做过交换钳制，这里再兜底一次防御性排序）。
    """
    settings = load_settings()
    try:
        min_len = int(settings.get("tts_min_segment_len", DEFAULT_SETTINGS["tts_min_segment_len"]))
    except (TypeError, ValueError):
        min_len = int(DEFAULT_SETTINGS["tts_min_segment_len"])
    try:
        max_len = int(settings.get("tts_max_segment_len", DEFAULT_SETTINGS["tts_max_segment_len"]))
    except (TypeError, ValueError):
        max_len = int(DEFAULT_SETTINGS["tts_max_segment_len"])
    if min_len > max_len:
        min_len, max_len = max_len, min_len
    return {"min_len": min_len, "max_len": max_len}


def get_subtitle_import_skip_split_every_n() -> int:
    """
    供 app.py 的字幕跟读路由（/api/subtitle-import/align）调用：实时读取
    "每多少个时间轴跳过分割音频"设置，转发给
    subtitle_import.align_subtitle_audio() 的 skip_split_every_n 参数。

    同样不使用 mtime 缓存（该路由调用频率远低于对齐热路径），每次直接
    调用 load_settings()，成本可忽略；保存设置后下一次字幕跟读任务立即
    生效，无需重启任何进程。

    Returns
    -------
    int，>= 1（1 表示逐条独立切分对齐，与改造前行为一致）。
    """
    settings = load_settings()
    try:
        n = int(settings.get(
            "subtitle_import_skip_split_every_n",
            DEFAULT_SETTINGS["subtitle_import_skip_split_every_n"],
        ))
    except (TypeError, ValueError):
        n = int(DEFAULT_SETTINGS["subtitle_import_skip_split_every_n"])
    return max(n, 1)


def get_tts_split_options() -> Dict[str, object]:
    """
    供 tts_processor.py 的 split_sentences() 调用：实时读取"分段"这一步
    相关的三个开关/参数（与 get_tts_segment_len() 分开，是因为那两个是
    "二次切割"的长度区间，这三个是"要不要按换行分段/ 按多少个换行分段 /
    要不要二次切割"本身的开关，语义上是两组不同的设置）。

    同样不使用 mtime 缓存，保存设置后下一次 split_sentences() 调用立即
    生效，无需重启任何进程。

    Returns
    -------
    Dict[str, object]，键：
      - "newline_split_every_n" (int)：每多少个换行才切一段，>= 1。
      - "disable_newline_split" (bool)：True 时完全不按换行分段。
      - "disable_segment_len_split" (bool)：True 时完全不做长度二次切割。
    """
    settings = load_settings()
    try:
        every_n = int(settings.get(
            "tts_newline_split_every_n", DEFAULT_SETTINGS["tts_newline_split_every_n"]
        ))
    except (TypeError, ValueError):
        every_n = int(DEFAULT_SETTINGS["tts_newline_split_every_n"])
    every_n = max(every_n, 1)
    return {
        "newline_split_every_n": every_n,
        "disable_newline_split": bool(settings.get("tts_disable_newline_split")),
        "disable_segment_len_split": bool(settings.get("tts_disable_segment_len_split")),
    }


def get_qwen3_batch_size() -> int:
    """
    供 alt_aligners.py（主进程内的 Qwen3ASRAligner / Qwen3ForcedAligner，
    现已在同一进程内本地加载模型）以及 nemo_server.py（独立进程）共用：
    实时读取 qwen3_batch_size 设置。

    与 get_alignment_tuning() 使用的 mtime 缓存不同，这里同 whisperx 的
    _get_whisperx_batch_size() 一样直接读盘——nemo_server.py 是独立子
    进程，调用频率远低于主进程的对齐热路径，没有必要为此额外维护一层
    跨进程都要生效的缓存；直接读盘成本可忽略，且保证"设置页面保存后
    下一次任务立即生效"（nemo_server.py 每次 /align 请求都会重新调用，
    不需要重启这个微服务；主进程内的 Qwen3-ASR/Qwen3-FA 同样每次调用时
    实时读取，不需要重启整个程序）。

    读取失败或配置值非法时安全回退到默认值 8。

    Returns
    -------
    int，>= 1。
    """
    try:
        settings = load_settings()
        val = int(settings.get("qwen3_batch_size", DEFAULT_SETTINGS["qwen3_batch_size"]))
        return max(1, val)
    except Exception:
        return int(DEFAULT_SETTINGS["qwen3_batch_size"])


def get_qwen3_tts_model_size() -> str:
    """
    供 qwen3tts_server.py（独立子进程）读取：实时读取用户在设置页选择的
    Qwen3-TTS 模型规模（"1.7B" / "0.6B"）。与 get_qwen3_batch_size() 一样
    直接读盘，不做跨进程缓存——qwen3tts_server.py 每次 /load 或懒加载时
    重新读取一次即可，调用频率远低于对齐热路径。

    读取失败或值非法时安全回退到默认值 "1.7B"。
    """
    try:
        settings = load_settings()
        val = str(settings.get("qwen3_tts_model_size") or "").strip()
        return val if val in ("1.7B", "0.6B") else str(DEFAULT_SETTINGS["qwen3_tts_model_size"])
    except Exception:
        return str(DEFAULT_SETTINGS["qwen3_tts_model_size"])


def get_qwen3_tts_x_vector_only_default() -> bool:
    """供前端"语音预设管理"新建 Voice Clone 预设时的默认勾选状态使用。"""
    try:
        settings = load_settings()
        return bool(settings.get("qwen3_tts_x_vector_only_default"))
    except Exception:
        return bool(DEFAULT_SETTINGS["qwen3_tts_x_vector_only_default"])


def get_output_timeline_files_enabled() -> bool:
    """
    供 tsubaki_processor.py（对话文本框批量处理 build_multitrack_project）
    和 tts_processor.py（TTS跟读 align_segments）共用：实时读取"是否额外
    输出序列时间表调试文件"总开关（.dtslt / .ttslt，均为 JSON）。

    与 get_qwen3_batch_size() 等同类只读设置一样直接读盘、不做缓存——
    这两个功能的调用频率远低于对齐热路径，直接读盘成本可忽略，且保证
    "设置页面保存后下一次任务立即生效"，无需重启任何进程。

    读取失败时安全回退到默认值 False（不生成调试文件）。
    """
    try:
        settings = load_settings()
        return bool(settings.get("output_timeline_files"))
    except Exception:
        return bool(DEFAULT_SETTINGS["output_timeline_files"])
