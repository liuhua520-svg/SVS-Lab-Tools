# -*- coding: utf-8 -*-
"""
音频标注和音高处理模块 - 工程文件生成引擎
用于处理 LAB 标注文件和音频数据，生成 Synthesizer V / OpenUtau 工程文件
"""
from __future__ import annotations

import json
import logging
import re
import numpy as np
import multiprocessing as mp
import traceback
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf

try:
    import pyworld as pw
except ImportError:
    pw = None

logger = logging.getLogger(__name__)


@dataclass
class AudioFrame:
    """音频帧数据"""
    start_time: int  # 100ns 单位
    end_time: int
    frequency: float  # F0 频率
    confidence: float = 1.0


@dataclass
class LabelSegment:
    """标注段"""
    start_time: int  # 100ns 单位
    end_time: int
    label: str
    # 可选：VOCALOID 风格辅音起始音素（例如 's'、'sh'、'ky'），仅在
    # "仅生成工程"模式下、且用户开启了日语音素锁定开关（ja_devoiced_phoneme）
    # 时由 apply_phoneme_mode(..., with_devoiced_phoneme=True) 填充。
    # 单独元音 / ん・ン・N / っ・ッ / 静音段没有辅音起始 → 保持 None，
    # 届时 VSQX <p> 字段留空，交给 VOCALOID 自带 G2P 处理。
    devoiced_phoneme: Optional[str] = None


@dataclass
class AudioProcessingConfig:
    """音频处理配置"""
    # 基本参数
    bpm: float = 120.0
    base_pitch: int = 60  # MIDI note，60 = C4

    # F0 提取参数
    f0_floor: float = 71.0
    f0_ceil: float = 800.0
    f0_method: str = "dio"  # 'dio' / 'harvest' / 'crepe' / 'rmvpe'

    # CREPE / RMVPE 运行设备："auto"（自动选择 cuda，否则 cpu）/ "cpu" / "cuda"
    f0_device: str = "auto"

    # CREPE 模型规格："full"（精度高）或 "tiny"（速度快）
    crepe_model: str = "full"

    # F0 细化参数
    f0_smooth: bool = True
    f0_smooth_window: int = 11   # 11 frames × 5ms = 55ms

    # 是否细化音高（控制 LAB 音符音高，是否用 F0 中位音高决定 tone）
    refine_pitch: bool = False

    # 是否将 F0 曲线写入工程文件
    export_pitch_line: bool = True

    # VSQX PIT（音高弯曲）曲线平滑窗口大小（帧数，0/1 = 不平滑）。
    # 独立于 f0_smooth_window：后者服务于音符音高判定（refine_pitch），
    # 前者只作用于最终写入 VSQX 的 <cc><v id="P"> 曲线，窗口越大越平滑。
    vsqx_pitch_smooth_window: int = 5

    use_double_precision: bool = False

    # 新增：默认关闭 d4c 硬筛，避免把有效 F0 删空
    enable_ap_check: bool = False

    def to_dict(self) -> Dict:
        return {
            "bpm": self.bpm,
            "base_pitch": self.base_pitch,
            "f0_floor": self.f0_floor,
            "f0_ceil": self.f0_ceil,
            "f0_method": self.f0_method,
            "f0_device": self.f0_device,
            "crepe_model": self.crepe_model,
            "f0_smooth": self.f0_smooth,
            "f0_smooth_window": self.f0_smooth_window,
            "refine_pitch": self.refine_pitch,
            "export_pitch_line": self.export_pitch_line,
            "vsqx_pitch_smooth_window": self.vsqx_pitch_smooth_window,
            "use_double_precision": self.use_double_precision,
            "enable_ap_check": self.enable_ap_check,
        }

# ---------------------------------------------------------------------------
# 100 nanosecond / blick 换算常量
# Synthesizer V 的 blick 单位即 100 纳秒（绝对时间，与 BPM 无关）
# 即: blick 值 = LAB 时间戳（100ns 单位）
# ---------------------------------------------------------------------------
_BLICKS_PER_SECOND = 10_000_000      # 1 秒 = 10,000,000 blicks (100ns)
_TICKS_PER_SECOND_DEFAULT = 480.0    # USTX default resolution per quarter note




def _split_lyrics_to_words(text: str) -> List[str]:
    """
    将歌词文本拆分为按音符对应的字/词列表。

    规则：
    - CJK 汉字、平假名、片假名、韩文 → 每个字符单独一个元素
    - 单独的 "-" 保留为占位符
    - 拉丁字母等 → 以空白符为分隔符拆成单词
    - 空格、其他标点、换行跳过
    """
    import unicodedata

    result: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        cp = ord(ch)

        if (
            (0x4E00 <= cp <= 0x9FFF)
            or (0x3400 <= cp <= 0x4DBF)
            or (0x20000 <= cp <= 0x2A6DF)
            or (0x3040 <= cp <= 0x309F)
            or (0x30A0 <= cp <= 0x30FF)
            or (0xAC00 <= cp <= 0xD7A3)
            or (0x1100 <= cp <= 0x11FF)
        ):
            result.append(ch)
            i += 1
        elif ch in (' ', '\t', '\n', '\r'):
            i += 1
        elif ch == '-':
            result.append('-')
            i += 1
        elif unicodedata.category(ch).startswith('P'):
            i += 1
        else:
            j = i
            while j < len(text) and text[j] not in (' ', '\t', '\n', '\r'):
                j += 1
            word = text[i:j].strip()
            if word:
                result.append(word)
            i = j

    return result


class TsubakiProcessor:
    """
    工程文件生成器。

    SVP 正确结构（参照 Synthesizer V 实际格式）：
    ─────────────────────────────────────────────────────────────────────
    • library[0]
        uuid   = group_uuid
        notes  = 全部音符（包含从 LAB 读入的 '-' consonant 音符 +
                  填充 'sil/sp' 间隙的 '-' 音符）
        parameters.pitchDelta.mode  = "cubic"
        parameters.pitchDelta.points = [pos, float_cents, ...]   ← 浮点

    • tracks[0]
        mainGroup  = {uuid=main_uuid, notes=[], parameters=<全空 cubic 曲线>}
        mainRef    = {groupID=main_uuid, ...}        ← 指向空 mainGroup
        groups[0]  = {groupID=group_uuid, ...}       ← 指向 library[0]

    USTX 修复要点：
    ─────────────────────────────────────────────────────────────────────
    • ruamel.yaml ≥ 0.18 已弃用 yaml_set_tag()，改用：
        part.yaml_set_ctag(Tag(suffix="!UVoicePart"))
    """

    SUPPORTED_FORMATS = {
        "sv":   "Synthesizer V Project (.svp)",
        "ustx": "OpenUtau Project (.ustx)",
        "utau": "OpenUtau Project (.ustx) [alias]",
        "midi": "MIDI 标准文件 (.mid)",
        "vsqx": "VOCALOID 4 Project (.vsqx)",
    }

    OUTPUT_ALIASES = {
        "svp":           "sv",
        "synthv":        "sv",
        "synthesizer_v": "sv",
        "synthesizerv":  "sv",
        "openutau":      "ustx",
        "utau":          "ustx",
        "mid":           "midi",
        "vsq4":          "vsqx",
        "vocaloid":      "vsqx",
        "vocaloid4":     "vsqx",
    }

    # 真正的静音标签：跳过（不生成音符），但用 '-' 填充其占用的时间段
    _TRUE_SILENCE = {"pau", "sil", "sp", "spn", "br", "silence", "noise", "ap", "blank"}

    def __init__(self, work_dir: str):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------
    # 基础工具
    # ----------------------------
    @staticmethod
    def _normalize_output_format(output_format: str) -> str:
        fmt = (output_format or "").strip().lower()
        fmt = TsubakiProcessor.OUTPUT_ALIASES.get(fmt, fmt)
        return fmt

    @staticmethod
    def _midi_to_freq(midi_note: int) -> float:
        return 440.0 * (2 ** ((midi_note - 69) / 12))

    @staticmethod
    def _freq_to_midi(freq: float) -> float:
        return 69 + 12 * np.log2(float(freq) / 440.0)

    @staticmethod
    def _lab_time_to_seconds(lab_time: int) -> float:
        return float(lab_time) / 10_000_000.0

    @staticmethod
    def _midi_notes_overlap_segment(
        start_sec: float,
        end_sec: float,
        midi_notes: List[Tuple[float, float, int]],
        min_overlap_sec: float = 0.01,
    ) -> bool:
        """
        判断 [start_sec, end_sec) 是否与 midi_notes 中的某个音符存在重叠。

        用途：批量顺序合并（build_multitrack_project）会把多个对话框的段落
        拼接到同一条时间轴，其中只有部分对话框可能提供了 MIDI（其余对话框
        走 LAB + F0 细化 / base_pitch）。若只用 "midi_notes is not None" 判断
        是否走 MIDI 音高分支，会导致合并后 midi_notes 变成非空列表，进而让
        本不属于任何 MIDI 对话框的段落也被"劫持"进入 MIDI 分支——而这些段落
        与 midi_notes 里的音符毫无时间重叠，map_segment_to_midi_pitch 会
        静默地退回 base_pitch，而不是它们本该使用的 F0 细化音高。
        这里先显式检查真实重叠，只有确有重叠时才使用 MIDI 音高，否则让
        调用方继续走 refine_pitch / base_pitch 分支（单一对话框场景下
        midi_notes 覆盖整条音轨，本检查恒为 True，不影响原有行为）。
        """
        if not midi_notes:
            return False
        for m_start, m_end, _pitch in midi_notes:
            if m_end < start_sec:
                continue
            if m_start > end_sec:
                break
            if min(end_sec, m_end) - max(start_sec, m_start) >= min_overlap_sec:
                return True
        return False

    @staticmethod
    def _is_true_silence(label: str) -> bool:
        """判断是否为真正的静音段（会被 '-' 填充，不生成可见音符）。
        注意：'-' 本身不是静音，它是 consonant phoneme onset 标记，应保留为音符。
        """
        return label.strip().lower() in TsubakiProcessor._TRUE_SILENCE

    # 匹配纯 ASCII 字母 + 撇号组成的英语单词（'t, I'm 等缩写）
    _ASCII_WORD_RE = re.compile(r"^[a-zA-Z][a-zA-Z']*$")

    @staticmethod
    def _is_ascii_word_label(label: str) -> bool:
        """
        判断 label 是否为可能的英语单词（非静音、非辅音占位符、纯 ASCII 字母）。

        用于 word_phoneme_map 开关：只对英语单词调用 word_to_arpabet()，
        跳过 '-' 占位符、CJK 字符段、pinyin/jyutping/hangul 音素段。

        注意：单纯的 ASCII 字母检查无法完全区分 "en 模式下的音素 hh/sh"
        和 "英语词 hello"——但 word_phoneme_map 只在 english_word_align=True
        时才启用，后者保证了 LAB 中的英语词以完整词（而非音素序列）形式存在，
        因此在实际场景中误判概率极低。
        """
        if not label or label == "-":
            return False
        if label.strip().lower() in TsubakiProcessor._TRUE_SILENCE:
            return False
        return bool(TsubakiProcessor._ASCII_WORD_RE.match(label))

    @staticmethod
    def _label_is_english_word(
        label: str,
        language: str,
        native_english_words: Optional[set] = None,
    ) -> bool:
        """
        判断 label 在给定语言上下文中是否应作为英语单词进行音素映射。

        逻辑
        ────
        · 语言为英语 (en/eng)：信任 _is_ascii_word_label，走完整 G2P 流程
          （MFA 词典 + g2p_en OOV 兜底），与旧行为完全一致。
        · 非英语语言（zh/ja/ko/yue/…）：
          a. 若 native_english_words 不为 None（由调用方从原始汉字/韩文文本
             预提取），则以该集合为唯一判断依据：
               - 原始文本里没有拉丁字母 → 集合为空 → 所有 label 均不命中
               - 混入了 "love/hello" 等真实英文词 → 只有这些词命中
             优势：彻底消除"拼音碰巧是英语词"的误判（rang/wang/dong/shi 等）。
          b. native_english_words 为 None（兼容旧调用路径，如 project-only 模式
             未传原始文本）：回退到词典查询（is_in_english_dict），保持向后兼容。
        · 词典文件缺失时，非英语语言一律返回 False（保守策略）。
        """
        lang = (language or "").lower().strip().rstrip("-")
        if lang in ("en", "eng", "english"):
            return True          # English: full G2P pipeline

        # ── 非英语语言 ────────────────────────────────────────────────────
        if native_english_words is not None:
            # 优先路径：用从原始文本预提取的集合判断（最准确）
            clean = (label or "").strip().lower()
            return bool(clean) and clean in native_english_words

        # 兼容回退：词典查询（旧行为）
        try:
            from phoneme_converter import is_in_english_dict
            return is_in_english_dict(label)
        except Exception:
            return False

    def _read_audio_duration_sec(self, wav_path: str) -> float:
        try:
            info = sf.info(wav_path)
            if info.samplerate <= 0:
                return 0.0
            return float(info.frames) / float(info.samplerate)
        except Exception:
            return 0.0

    def _load_lab_segments(self, lab_path: str) -> List[LabelSegment]:
        segments: List[LabelSegment] = []
        with open(lab_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                try:
                    start_time = int(float(parts[0]))
                    end_time = int(float(parts[1]))
                except Exception:
                    continue
                label = " ".join(parts[2:]).strip()
                segments.append(LabelSegment(start_time=start_time, end_time=end_time, label=label))
        return segments

    @staticmethod
    def _segments_from_midi_notes(
        midi_notes: List[Tuple[float, float, int]],
        label: str = "-",
        lyric_words: Optional[List[str]] = None,
    ) -> List[LabelSegment]:
        """
        将 MIDI 音符列表转换为 LabelSegment 列表。

        每个 MIDI 音符对应一个 LabelSegment。
        音高信息由调用处通过 midi_notes + map_segment_to_midi_pitch 获取，
        不需要编码在 label 内。

        Parameters
        ----------
        midi_notes : list of (start_sec, end_sec, pitch)
        label : str
            未提供 lyric_words 或超出长度时的兜底歌词，默认 '-'
        lyric_words : list of str, optional
            按音符顺序对应的歌词字/词列表。提供时逐一写入对应音符；
            列表比音符少时，超出部分使用 label 填充。
        """
        segments: List[LabelSegment] = []
        for i, (start_sec, end_sec, _pitch) in enumerate(midi_notes):
            start_100ns = int(round(float(start_sec) * 10_000_000))
            end_100ns   = int(round(float(end_sec)   * 10_000_000))
            if end_100ns > start_100ns:
                note_label = (
                    lyric_words[i]
                    if lyric_words and i < len(lyric_words)
                    else label
                )
                segments.append(LabelSegment(
                    start_time=start_100ns,
                    end_time=end_100ns,
                    label=note_label,
                ))
        return segments

    def _default_svp_note(self, lyric: str, tone: int, onset: int = 0, duration: int = 0) -> Dict:
        """生成 SVP 音符（使用 SV 标准属性格式，去除 3.x 独有字段）。"""
        return {
            "onset": onset,
            "duration": max(1, duration),
            "lyrics": lyric,
            "phonemes": "",
            "pitch": int(tone),
            "detune": 0,
            "attributes": {
                "tF0Offset": 0,
                "tF0Left":   0,
                "tF0Right":  0,
                "dF0Left":   0,
                "dF0Right":  0,
                "dF0Vbr":    0,
            },
        }

    def _empty_svp_parameters(self) -> Dict:
        """生成全空 SVP 参数曲线（用于 mainGroup）。"""
        empty = {"mode": "cubic", "points": []}
        return {
            "pitchDelta":  dict(empty),
            "vibratoEnv":  dict(empty),
            "loudness":    dict(empty),
            "tension":     dict(empty),
            "breathiness": dict(empty),
            "voicing":     dict(empty),
            "gender":      dict(empty),
            "toneShift":   dict(empty),
        }

    def _group_ref(self, group_uuid: str) -> Dict:
        """生成 group 引用结构（mainRef / groups 元素通用）。"""
        return {
            "groupID":        group_uuid,
            "blickOffset":    0,
            "pitchOffset":    0,
            "isInstrumental": False,
            "database": {
                "name":        "",
                "language":    "",
                "phoneset":    "",
                "backendType": "",
            },
            "dictionary": "",
            "voice":      {},
        }

    def process_full(self, *args, **kwargs):
        return self.process_full_pipeline(*args, **kwargs)

    # ----------------------------
    # F0 后处理工具
    # ----------------------------
    # 说明：本类此前存在两套同名的 _median_filter_1d /
    # _contiguous_runs / _soft_reject_spikes / _post_process_f0
    # （一套重构前、一套重构后），Python 类体按定义顺序后者覆盖前者，
    # 旧的一套从未被实际调用过，属于重构遗留的死代码，此处已删除，
    # 只保留下面这套（新版，含 log2 域去尖峰）。
    # ----------------------------
    @staticmethod
    def _median_filter_1d(arr: np.ndarray, kernel_size: int) -> np.ndarray:
        """纯 numpy 1D 中值滤波（无需 scipy）。"""
        arr = np.asarray(arr, dtype=np.float64)
        if arr.size == 0:
            return arr.copy()

        k = int(kernel_size)
        if k <= 1:
            return arr.copy()
        if k % 2 == 0:
            k += 1

        pad = k // 2
        padded = np.pad(arr, pad, mode="edge")

        try:
            windows = np.lib.stride_tricks.sliding_window_view(padded, k)
        except AttributeError:
            strides = (padded.strides[0], padded.strides[0])
            windows = np.lib.stride_tricks.as_strided(
                padded,
                shape=(arr.size, k),
                strides=strides,
            )

        return np.median(windows, axis=-1)

    @staticmethod
    def _moving_average_1d(arr: np.ndarray, kernel_size: int) -> np.ndarray:
        """纯 numpy 1D 均值平滑。"""
        arr = np.asarray(arr, dtype=np.float64)
        if arr.size == 0:
            return arr.copy()

        k = int(kernel_size)
        if k <= 1:
            return arr.copy()
        if k % 2 == 0:
            k += 1

        pad = k // 2
        padded = np.pad(arr, (pad, pad), mode="edge")
        kernel = np.ones(k, dtype=np.float64) / float(k)
        return np.convolve(padded, kernel, mode="valid")

    @staticmethod
    def _hann_smooth_1d(arr: np.ndarray, kernel_size: int) -> np.ndarray:
        """
        使用 Hann 窗进行居中平滑，并保持输入长度不变。

        VSQX 的 PIT 控制点在编辑器中按阶梯保持，而不是自动绘制成连续曲线。
        使用 Hann 窗可减少箱式移动平均造成的
        高频旁瓣，同时不会像大窗口块平均那样把真实音高走势压成宽台阶。
        """
        arr = np.asarray(arr, dtype=np.float64)
        if arr.size == 0:
            return arr.copy()

        k = int(kernel_size)
        if k <= 1 or arr.size < 3:
            return arr.copy()
        if k % 2 == 0:
            k += 1
        if k > arr.size:
            k = int(arr.size)
            if k % 2 == 0:
                k -= 1
        if k < 3:
            return arr.copy()

        weights = np.hanning(k)
        weight_sum = float(weights.sum())
        if weight_sum <= 0:
            return arr.copy()

        pad = k // 2
        padded = np.pad(arr, (pad, pad), mode="edge")
        return np.convolve(padded, weights / weight_sum, mode="valid")

    @staticmethod
    def _contiguous_runs(mask: np.ndarray) -> List[Tuple[int, int]]:
        idx = np.flatnonzero(mask)
        if idx.size == 0:
            return []
        splits = np.where(np.diff(idx) > 1)[0] + 1
        groups = np.split(idx, splits)
        return [(int(g[0]), int(g[-1]) + 1) for g in groups if len(g) > 0]

    @staticmethod
    def _correct_octave_errors(
        f0: np.ndarray,
        voiced: np.ndarray,
        local_win: int = 31,
        tol: float = 0.25,
    ) -> np.ndarray:
        """
        检测并纠正倍频/半频误跟踪（octave jump），这是 DIO/Harvest 一类
        DSP 音高提取器最典型的失败模式之一：某一段帧被错误地跟踪到
        真实音高的 2 倍或 1/2（偶尔 3 倍/1/3），在 log2 domain 里表现为
        与邻域中位数正好相差 ±1（或 ±log2(3)）个八度。

        【为什么普通中值滤波不够】_median_filter_1d 去尖峰依赖"异常值
        在窗口内占少数"这个假设：核大小为 N 时只能纠正连续不超过
        (N-1)//2 帧的异常。但真实的倍频误判经常持续 20-50ms（4-10 帧
        @ 5ms/帧），一旦超过窗口能扛住的长度，中值本身就落在错误值
        上，之后的 moving average 只会在"错误值"和"正确值"之间画一条
        平滑但依然错误的过渡曲线——看起来顺滑，实际音高是错的。

        【做法】octave 误判有一个中值滤波没有利用到的强特征：误差幅度
        几乎精确等于整数个八度，而不是任意随机幅度。这里用一个明显
        更宽的邻域窗口（默认 31 帧 ≈ 150ms，足以跨越常见的误判持续
        时长，让邻域中位数仍然由"多数正确帧"主导）计算 log2 音高的
        局部中位数，只要某一帧与该中位数的差值落在 (0.75, 1.25) 个
        八度或 (log2(3)-tol, log2(3)+tol) 个八度范围内，就按最近的整数
        八度（或 3 倍）纠正，而不是依赖窗口"扛过去"。

        真实的大幅音高变化（比如滑音/甩音跨越 5-6 个半音≈0.4-0.5
        个八度）远小于 0.75 个八度的检测下限，不会被误纠正。
        """
        f0 = np.asarray(f0, dtype=np.float64)
        out = f0.copy()
        if voiced.sum() < 5:
            return out

        f0_log = np.full(f0.shape, np.nan, dtype=np.float64)
        f0_log[voiced] = np.log2(np.maximum(f0[voiced], 1e-12))

        half = max(3, int(local_win) // 2)
        # 允许纠正的倍数：±1 个八度（2x/0.5x）与 ±log2(3) 个八度（3x/(1/3)x）
        candidate_octaves = [1.0, np.log2(3.0)]

        for s, e in TsubakiProcessor._contiguous_runs(voiced):
            n = e - s
            if n < 5:
                continue
            for i in range(s, e):
                lo, hi = max(s, i - half), min(e, i + half + 1)
                # 邻域中位数排除自身，避免异常帧本身拉偏中位数（尤其是
                # 误判段落靠近邻域窗口一端时）
                local_slice = np.concatenate([
                    f0_log[lo:i], f0_log[i + 1:hi]
                ])
                local_slice = local_slice[np.isfinite(local_slice)]
                if local_slice.size < 5:
                    continue
                local_med = float(np.median(local_slice))
                delta = f0_log[i] - local_med
                for oct_val in candidate_octaves:
                    if abs(abs(delta) - oct_val) <= tol:
                        sign = 1.0 if delta > 0 else -1.0
                        out[i] = f0[i] / (2.0 ** (sign * oct_val))
                        break
        return out

    def _soft_reject_spikes(
        self,
        f0: np.ndarray,
        suspicious: np.ndarray,
        max_jump_semitones: float = 3.0,
        local_win: int = 5,
    ) -> np.ndarray:
        """只处理疑似擦音假峰，不扩大影响范围。"""
        out = np.array(f0, dtype=np.float64, copy=True)
        voiced = out > 0
        if voiced.sum() < 3:
            return out

        midi = np.full(out.shape, np.nan, dtype=np.float64)
        midi[voiced] = 69.0 + 12.0 * np.log2(np.maximum(out[voiced], 1e-12) / 440.0)

        runs = self._contiguous_runs(voiced)
        for s, e in runs:
            half = max(1, local_win // 2)
            for i in range(s, e):
                if not suspicious[i] or not np.isfinite(midi[i]):
                    continue

                left = max(s, i - half)
                right = min(e, i + half + 1)
                win = midi[left:right]
                win = win[np.isfinite(win)]
                if win.size < 3:
                    continue

                local_med = float(np.nanmedian(win))
                if abs(midi[i] - local_med) >= max_jump_semitones:
                    out[i] = 0.0

        return out

    def _post_process_f0(self, f0: np.ndarray, config: AudioProcessingConfig) -> np.ndarray:
        """
        后处理：
        - f0_smooth=False: 直接原样返回，不做平滑
        - f0_smooth=True : 先去尖峰，再做 log2 域平滑
        """
        f0 = np.asarray(f0, dtype=np.float64).copy()
        if f0.size == 0:
            return f0

        # 关闭平滑时：尽量保留原样
        if not config.f0_smooth:
            return f0

        # 只在开启平滑时做清理与平滑
        f0[~np.isfinite(f0)] = 0.0
        f0[(f0 < config.f0_floor * 0.6) | (f0 > config.f0_ceil * 1.15)] = 0.0

        voiced = f0 > 0
        runs = self._contiguous_runs(voiced)

        # 只补很短的断裂
        for (s1, e1), (s2, e2) in zip(runs, runs[1:]):
            gap = s2 - e1
            if 1 <= gap <= 3 and f0[e1 - 1] > 0 and f0[s2] > 0:
                left = np.log2(max(float(f0[e1 - 1]), 1e-12))
                right = np.log2(max(float(f0[s2]), 1e-12))
                bridge = np.linspace(left, right, gap + 2, dtype=np.float64)[1:-1]
                f0[e1:s2] = np.power(2.0, bridge)

        # 软去尖峰
        suspicious = (f0 >= config.f0_ceil * 0.92) & (f0 > 0)
        f0 = self._soft_reject_spikes(f0, suspicious, max_jump_semitones=3.0, local_win=5)

        voiced = f0 > 0
        runs = self._contiguous_runs(voiced)
        out = np.zeros_like(f0)

        for s, e in runs:
            seg = np.array(f0[s:e], dtype=np.float64, copy=True)
            n = len(seg)

            if n < 3:
                out[s:e] = np.clip(seg, config.f0_floor, config.f0_ceil)
                continue

            # 先中值滤波去毛刺
            if n >= 5:
                seg = self._median_filter_1d(seg, 3)

            # 再在 log2 域做平滑
            win = int(config.f0_smooth_window)
            win = max(5, win)
            if win % 2 == 0:
                win += 1
            if win > n:
                win = n if n % 2 == 1 else n - 1

            if win >= 5 and n >= win:
                log_seg = np.log2(np.maximum(seg, 1e-12))
                log_seg = self._moving_average_1d(log_seg, win)
                seg = np.power(2.0, log_seg)

            out[s:e] = np.clip(seg, config.f0_floor, config.f0_ceil)

        return out

    # ----------------------------
    # F0 提取
    # ----------------------------
    def process_audio_f0(self, audio_path: str, config: AudioProcessingConfig) -> Dict:
        """提取基频 F0。

        支持四种方法：
            - dio / harvest : PyWORLD（本地 DSP 算法，速度快，无额外依赖）
            - crepe         : torchcrepe 神经网络（抗噪，需要 torch + torchcrepe）
            - rmvpe         : RMVPE 深度模型（对人声极为鲁棒，需要 torch + 模型权重）

        无论使用哪种方法，统一返回 {"success": True, "f0": ndarray, "t": ndarray, "sr": int}，
        其中 f0 为 Hz（未发声 = 0），t 为对应时间戳（秒）。该结果可直接传入
        _build_svp_project_text / _build_utau_project_text 写入 pitchDelta / pitd 曲线。
        """
        import numpy as np
        import soundfile as sf

        method = (config.f0_method or "dio").strip().lower()

        try:
            x, sr = sf.read(audio_path)
            if x.ndim > 1:
                x = np.mean(x, axis=1)
            x = x.astype(np.float64)

            if method == "harvest":
                import pyworld as pw
                f0, t = pw.harvest(
                    x, sr,
                    f0_floor=config.f0_floor, f0_ceil=config.f0_ceil,
                    frame_period=5.0
                )

            elif method == "crepe":
                from f0_extractors import extract_f0_crepe
                f0, t = extract_f0_crepe(
                    x, sr,
                    f0_floor=config.f0_floor,
                    f0_ceil=config.f0_ceil,
                    model_size=config.crepe_model,
                    device=config.f0_device,
                )

            elif method == "rmvpe":
                from f0_extractors import extract_f0_rmvpe
                f0, t = extract_f0_rmvpe(
                    x, sr,
                    f0_floor=config.f0_floor,
                    f0_ceil=config.f0_ceil,
                    device=config.f0_device,
                )

            else:
                # 默认 / 'dio'
                import pyworld as pw
                _f0, t = pw.dio(
                    x, sr,
                    f0_floor=config.f0_floor, f0_ceil=config.f0_ceil,
                    frame_period=5.0
                )
                f0 = pw.stonemask(x, _f0, t, sr)

            # 统一类型
            f0 = np.asarray(f0, dtype=np.float64)
            t  = np.asarray(t,  dtype=np.float64)

            # 线性插值无声段（仅当存在足够的有声帧时）
            voiced_idx = np.nonzero(f0 > 0)[0]
            if len(voiced_idx) > 1:
                unvoiced_idx = np.where(f0 == 0)[0]
                if len(unvoiced_idx) > 0:
                    f0[unvoiced_idx] = np.interp(
                        unvoiced_idx, voiced_idx, f0[voiced_idx]
                    )

            # 可选平滑：① 倍频/半频误判纠正 → ② log2（半音）域中值滤波
            # 去尖峰 → ③ 移动平均。
            #
            # 【修复】原实现直接在 Hz 域对原始值做移动平均，没有去尖峰
            # 步骤——均值滤波对孤立尖峰没有抑制能力，尖峰只会被摊薄扩散
            # 到相邻几帧而不是被消除，表现为"开了平滑窗口但仍有锯齿"。
            # 同时 Hz 域平均本身在感知上也不准确（音高感知是对数关系），
            # 这里改为与 VSQX PIT 曲线平滑（_build_vsqx_part_xml）一致的
            # "log2 域中值去尖峰 + 移动平均"，避免两处平滑策略不一致导致
            # 叠加后行为难以预测（例如两个平滑窗口都调大时被压缩两次）。
            #
            # 【进一步修复：倍频/半频误判专项纠正】中值滤波去尖峰依赖
            # "异常值在窗口内占少数"这个假设：核大小为 N 时只能纠正连续
            # 不超过 (N-1)//2 帧的异常。但倍频/半频误判（DIO/Harvest 这类
            # DSP 音高提取器最典型的失败模式之一）经常连续持续 20-50ms
            # （4-10 帧 @ 5ms/帧），一旦超过中值滤波窗口能扛住的长度，
            # 中值本身就落在错误值上，之后的 moving average 只会在错误值
            # 和正确值之间画一条平滑但依然错误的过渡曲线——看起来顺滑，
            # 实际音高是错的（实测：合成信号里连续 8 帧的半频误判，仅靠
            # 中值滤波+移动平均完全未被修正）。
            #
            # 倍频误判有一个中值滤波没有利用到的强特征：误差幅度几乎
            # 精确等于整数个八度（2x/0.5x，偶尔 3x/(1/3)x），而不是任意
            # 随机幅度；真实的大幅音高变化（滑音/甩音跨越 5-6 个半音，
            # 约 0.4-0.5 个八度）远小于八度检测的下限，不会被误纠正。
            # 因此在中值滤波之前先用更宽的邻域窗口（150ms 量级，足以
            # 跨越常见误判持续时长，让邻域中位数仍由多数正确帧主导）
            # 专门检测并按整数八度直接纠正，不再依赖"中值滤波窗口能否
            # 扛过去"这种和误判段落长度赛跑的运气。
            if config.f0_smooth and len(f0) > 2:
                win = int(config.f0_smooth_window)
                if win % 2 == 0:
                    win += 1
                if win > 1:
                    voiced = f0 > 0
                    if np.any(voiced):
                        # ① 倍频/半频误判专项纠正（先于去尖峰/平滑）
                        f0 = self._correct_octave_errors(f0, voiced)

                        f0_log = np.zeros_like(f0)
                        f0_log[voiced] = np.log2(f0[voiced])

                        # ② 去尖峰：中值滤波（仅在有声帧上，避免污染静音）。
                        # 窗口跟随平滑窗口自适应，能剔除连续多帧的异常值，
                        # 而不只是孤立单帧。此前上限固定在 9 点（能扛住
                        # 连续 4 帧异常），现在放宽到 11 点（能扛住连续
                        # 5 帧异常）——八度级的大误判已经交给上面的专项
                        # 纠正处理，这里的中值滤波窗口不必再为"扛住大
                        # 误判"而束手束脚，可以放宽一点去覆盖更多中等
                        # 幅度、中等持续时长的跳变，让"关闭/开启 f0_smooth"
                        # 在这类场景下的差异更容易被感知到。
                        despike_win = min(11, max(3, win // 2))
                        if despike_win % 2 == 0:
                            despike_win += 1
                        if voiced.sum() >= despike_win:
                            f0_log[voiced] = self._median_filter_1d(f0_log[voiced], despike_win)

                        # ③ 移动平均（log2 域）
                        padded = np.pad(f0_log, win // 2, mode="edge")
                        f0_log = np.convolve(padded, np.ones(win) / win, mode="valid")

                        f0_new = np.zeros_like(f0)
                        f0_new[voiced] = np.exp2(f0_log[voiced])
                        f0 = f0_new

            return {"success": True, "f0": f0, "t": t, "sr": sr, "method": method}

        except ImportError as e:
            logger.error(f"F0 提取失败（依赖缺失，method={method}）: {e}")
            return {
                "success": False,
                "error": f"F0 方法 '{method}' 所需依赖未安装: {e}",
            }
        except FileNotFoundError as e:
            logger.error(f"F0 提取失败（缺少模型权重，method={method}）: {e}")
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error("F0 提取失败:" + str(e), exc_info=True)
            return {"success": False, "error": str(e)}

    # ----------------------------
    # SVP 生成（完整修复版）
    # ----------------------------
    def _build_svp_track_payload(
        self,
        segments: List[LabelSegment],
        f0: Optional[np.ndarray],
        t: Optional[np.ndarray],
        config: AudioProcessingConfig,
        midi_notes: Optional[List] = None,
        word_phoneme_map: bool = False,
        language: str = "",
        native_english_words: Optional[set] = None,
        dict_source: str = "default",
    ) -> Tuple[List[Dict], List[float]]:
        """
        构建单条音轨的 notes 列表 + pitchDelta points（float cents）。

        供单音轨 (_build_svp_project_text) 与多音轨
        (build_svp_project_text_multitrack，对话文本框批量处理功能) 共用，
        避免逻辑重复导致行为不一致。

        Returns
        -------
        (all_notes, pitch_data)
        """
        base_tone = int(config.base_pitch)

        # ── 步骤 1：从 LAB segments 构建时间轴音符列表 ───────────────────────
        # 规则：
        #   '-'                → 保留（consonant onset 标记）
        #   sil / pau / sp     → 转换为 '-' dash 音符（填充间隙，使时间轴连续）
        #   其他（hai, da ...） → 保留

        offset_ratio = config.bpm * 705600000 / 60

        all_notes: List[Dict] = []
        voiced_refs: List[Dict] = []

        for seg in segments:
            if seg.end_time <= seg.start_time:
                continue

            # 后续的重构公式保持不变
            onset = int(seg.start_time * offset_ratio / 10000000)
            end_offset = int(seg.end_time * offset_ratio / 10000000)
            dur = max(1, end_offset - onset)

            # 【修改点】如果是 lab 里的显式静音标签（sil/pau/sp），直接跳过不生成音符，使其在 SVP 中保持物理空白
            if self._is_true_silence(seg.label):
                continue

            # 下面不再需要 else，原本 else 内部的代码直接靠左对齐正常运行即可
            tone = base_tone
            _start_sec = self._lab_time_to_seconds(seg.start_time)
            _end_sec   = self._lab_time_to_seconds(seg.end_time)
            _seg_has_midi = midi_notes is not None and self._midi_notes_overlap_segment(
                _start_sec, _end_sec, midi_notes
            )
            if _seg_has_midi:
                # ── MIDI 模式：直接从 MIDI 文件读取该段的音符音高 ──
                # （仅当该段时间范围内确有 MIDI 音符重叠时才采用；顺序合并
                # 多个对话框时，没有 MIDI 的对话框段落不会被其它对话框的
                # midi_notes 误判命中，见 _midi_notes_overlap_segment）
                from midi_processor import map_segment_to_midi_pitch
                tone = map_segment_to_midi_pitch(_start_sec, _end_sec, midi_notes,
                                                 base_pitch=base_tone)
            elif config.refine_pitch and t is not None and f0 is not None and len(f0) > 0:
                start_sec = _start_sec
                end_sec   = _end_sec
                mask      = (t >= start_sec) & (t < end_sec)
                seg_f0    = f0[mask]
                voiced    = seg_f0[seg_f0 > 0]
                if len(voiced) > 0:
                    # 在 MIDI（半音）空间取中位数，避免 Hz 域平均偏向高频
                    midi_vals = 69.0 + 12.0 * np.log2(voiced / 440.0)
                    tone      = int(round(float(np.median(midi_vals))))
                    tone      = max(12, min(127, tone))

            # 原本就有的 "-" 标签由于不属于 _is_true_silence，会顺利走到这里，被正确保留为 "-" 音符
            note = self._default_svp_note(seg.label, tone, onset, dur)

            # ── 日语去母音化音素写入（ja_devoiced_phoneme）──────────────────
            # seg.devoiced_phoneme 由 process_full_pipeline() 在 phoneme_mode
            # 为 merge/hiragana/katakana 且 ja_devoiced_phoneme=True 时，通过
            # apply_phoneme_mode(..., with_devoiced_phoneme=True) 预先算出，
            # 仅当该合并音节的元音在原始 LAB 中被标记为去母音化（大写 I/U）
            # 时才有值，为该音节对应的 VOCALOID4 官方去母音化音素符号
            # （据 VOCALOID4 Editor manual 附录音素表；见
            # phoneme_converter.ja_devoiced_onset_to_vocaloid4()）。多数行
            # 保持辅音本身（す→'s'、つ→'ts'），但き/ひ/ぴ有专属去母音化
            # 记号（き→"k'"、ひ→"C"、ぴ→"p'"），ふ固定为 "p\\"。有浊音的
            # 音节（如 ご/go）以及单独元音 / ん・ン・N / っ・ッ / 静音段的
            # devoiced_phoneme 恒为 None，此时 phonemes 字段保持默认（空），
            # 交给 SynthesizerV 自带发音引擎处理。
            # 与 VSQX 的 <p lock="1"> 不同：SVP 没有锁定字段，这里只写入
            # 音素本身；与英语 word_phoneme_map / 自定义词典路径完全解耦，
            # 各走各的，互不影响（下面的 elif 才是英语单词映射逻辑）。
            if getattr(seg, "devoiced_phoneme", None):
                note["phonemes"] = seg.devoiced_phoneme

            # ── 音素写入 phonemes 字段：词典命中 与 word_phoneme_map 开关解耦 ──
            # 条件：label 是 ASCII 形态（非静音/非 CJK 占位符）。
            #
            # 【解耦说明】选择了自定义词典（dict_source != "default"）时，只要
            # 词典命中该词，就应写入音素——不要求用户额外打开
            # "英语单词→音素映射"（word_phoneme_map）开关；该开关只控制"词典
            # 未命中时，是否兜底走软件默认 G2P（word_to_arpabet）转换全部英语
            # 单词"。因此这里的入口条件是"开关已开启 或 已选择自定义词典"，
            # 而不是像旧版那样把整段逻辑（含词典查询）都锁在开关之后。
            #
            # 【重要】词典查询本身不受 _label_is_english_word 语言守卫限制：
            # 该守卫（native_english_words / is_in_english_dict）判断的是
            # "这是否是一个真实的英语单词"，用于防止中文拼音 rang/wang/dong
            # 等被 g2p_en 误当英语词转换成 ARPABET——但自定义词典的条目不
            # 局限于真实英语单词（dictionary_manager.py 明确支持任意语言的
            # 字词，甚至可以是 ARPABET/VOCALOID 音素符号本身，例如跨语种
            # 英语在 MFA 里回退到音素级对齐时，seg.label 可能直接是
            # "ah"/"ch" 这样的音素符号而非完整拼写单词）。若仍然对词典查询
            # 套用这层语言守卫，会导致词典里明明存在的音素因为 label 不是
            # "真实英语单词"而被判定为未命中，音素因此没有被写入。因此语言
            # 守卫只应用于 G2P 兜底（word_to_arpabet），不应用于词典查询。
            _dict_selected = bool(dict_source) and dict_source != "default"
            if (
                not getattr(seg, "devoiced_phoneme", None)
                and (word_phoneme_map or _dict_selected)
                and self._is_ascii_word_label(seg.label)
            ):
                try:
                    resolved_phones: Optional[List[str]] = None

                    # ── 优先查询用户自定义词典（notation 为 "synthesizerv"，
                    # ARPABET 记号）── SVP 的 phonemes 字段本就要求 ARPABET
                    # 记号，因此这里只消费 notation="synthesizerv" 的词典；
                    # notation="vocaloid"（VOCALOID4 记号）的词典不适用于
                    # SVP，命中不到时照常回退到软件默认转换。
                    # 注意：判断记号要用词典的 notation 元数据，不能直接拿
                    # dict_source（词典名，用户自定义任意字符串）跟字面量
                    # "synthesizerv" 比较——词典名和记号是两回事。
                    if _dict_selected:
                        from dictionary_manager import get_notation as _dict_notation
                        from dictionary_manager import lookup_word as _dict_lookup
                        if _dict_notation(dict_source) == "synthesizerv":
                            user_phones_str = _dict_lookup(seg.label, dict_source)
                            if user_phones_str:
                                resolved_phones = user_phones_str.split()
                                logger.debug(
                                    "[SVP word_phoneme_map] 用户词典命中 %r → %s",
                                    seg.label, user_phones_str,
                                )

                    # G2P 兜底（word_to_arpabet）：仅在词典未命中、且用户开启了
                    # word_phoneme_map 开关时才走，并且必须通过语言守卫
                    # （_label_is_english_word），防止拼音误判——这是唯一
                    # 需要该守卫的地方。
                    if (
                        resolved_phones is None
                        and word_phoneme_map
                        and self._label_is_english_word(seg.label, language, native_english_words)
                    ):
                        from phoneme_converter import word_to_arpabet
                        resolved_phones = word_to_arpabet(seg.label)

                    if resolved_phones:
                        note["phonemes"] = " ".join(resolved_phones)
                        logger.debug(
                            "[SVP word_phoneme_map] %r → %s",
                            seg.label, note["phonemes"],
                        )
                except Exception as _wp_err:
                    logger.warning(
                        "[SVP word_phoneme_map] 转换失败 %r: %s", seg.label, _wp_err
                    )

            all_notes.append(note)
            if seg.label != "-":
                voiced_refs.append(note)

        # ── 步骤 2：检查并填补音符之间的空隙 ────────────────────────────────
        # （正常情况下 LAB 已连续，此步作为保险）
        all_notes.sort(key=lambda n: n["onset"])

        # ── 步骤 3：pitchDelta points（float cents，cubic mode）──────────────
        pitch_data: List[float] = []
        voiced_sample_count = 0

        logger.info(
            f"Pitch Export Debug: export_pitch_line={config.export_pitch_line}, "
            f"f0_len={0 if f0 is None else len(f0)}, "
            f"t_len={0 if t is None else len(t)}"
        )

        if config.export_pitch_line and t is not None and f0 is not None and len(f0) > 0:
            for ti, f0i in zip(t, f0):
                if not np.isfinite(f0i) or f0i <= 0:
                    continue

                voiced_sample_count += 1

                # 音频时间（秒）→ SV blick 轴
                pos = int(float(ti) * offset_ratio)

                nominal_tone = base_tone
                for vn in voiced_refs:
                    if vn["onset"] <= pos <= (vn["onset"] + vn["duration"]):
                        nominal_tone = int(vn["pitch"])
                        break

                pitch_midi = self._freq_to_midi(float(f0i))
                deviation_cents = float((pitch_midi - nominal_tone) * 100)

                pitch_data.extend([pos, deviation_cents])

            logger.info(
                f"Pitch Export Debug: valid_f0={voiced_sample_count}, "
                f"generated_points={len(pitch_data)//2}"
            )

            if not pitch_data:
                logger.warning("pitchDelta 为空：导出前的 F0 基本被清空了，写入零线兜底。")
                for note in all_notes:
                    pitch_data.extend([note["onset"], 0.0])
                    pitch_data.extend([note["onset"] + note["duration"], 0.0])

        return all_notes, pitch_data

    def _build_svp_project_text(
        self,
        title: str,
        segments: List[LabelSegment],
        f0: Optional[np.ndarray],
        t: Optional[np.ndarray],
        sr: Optional[int],
        wav_path: str,
        config: AudioProcessingConfig,
        audio_duration_sec: Optional[float] = None,
        midi_notes: Optional[List] = None,   # MIDI 导入：(start_sec, end_sec, pitch) 列表
        word_phoneme_map: bool = False,       # 英语单词 → ARPABET 音素写入 phonemes 字段
        language: str = "",                   # 语种（用于 word_phoneme_map 防误判）
        native_english_words: Optional[set] = None,  # 从原始文本预提取的英语单词集合（防拼音误判）
        dict_source: str = "default",         # 单词→音素词典来源："default"/"synthesizerv"/"vocaloid"
    ) -> str:
        """
        生成可被 Synthesizer V 正确识别的 SVP JSON 文本（单音轨）。

        修复要点
        ────────
        1. blicks = LAB 时间戳（100ns 单位）不可使用，即 blicks_per_second = 10,000,000。
           需要使用 (bpm/60)*705600000 换算，那个公式会让时间缩小 141 倍。

        2. 音符放入 library[0].notes，而非 mainGroup.notes：
           • '-' (consonant onset) 保留为实际音符（不跳过）
           • 'sil/pau/sp/spn' 真正静音段 → 用 '-' 填充其时间范围
           • 相邻有声音符之间如有剩余间隙 → 再补 '-' 填充

        3. mainGroup 保持空 notes；tracks[0].groups[0] 引用 library[0]。

        4. pitchDelta 格式：mode="cubic"，y 值使用 float cents（不取整）。

        5. time 结构去除 version 7 格式的顶层 bpm 字段，改为 version 119 格式。

        实际的音符 / 音高曲线构建逻辑已提取到 _build_svp_track_payload()，
        与对话文本框批量处理功能的多音轨生成器共用，本方法只负责组装
        单音轨的 library/tracks JSON 结构。
        """
        if audio_duration_sec is None:
            audio_duration_sec = 0.0

        import uuid

        all_notes, pitch_data = self._build_svp_track_payload(
            segments=segments,
            f0=f0,
            t=t,
            config=config,
            midi_notes=midi_notes,
            word_phoneme_map=word_phoneme_map,
            language=language,
            native_english_words=native_english_words,
            dict_source=dict_source,
        )

        # ── 组装 JSON ─────────────────────────────────────────────────
        main_uuid  = str(uuid.uuid4()).lower()
        group_uuid = str(uuid.uuid4()).lower()

        library_group = {
            "name": title,
            "uuid": group_uuid,
            "parameters": {
                "pitchDelta": {
                    "mode":   "cubic",
                    "points": pitch_data,
                },
                "vibratoEnv":  {"mode": "cubic", "points": []},
                "loudness":    {"mode": "cubic", "points": []},
                "tension":     {"mode": "cubic", "points": []},
                "breathiness": {"mode": "cubic", "points": []},
                "voicing":     {"mode": "cubic", "points": []},
                "gender":      {"mode": "cubic", "points": []},
                "toneShift":   {"mode": "cubic", "points": []},
            },
            "notes": all_notes,
        }

        main_group = {
            "name":       "main",
            "uuid":       main_uuid,
            "parameters": self._empty_svp_parameters(),
            "notes":      [],
        }

        project = {
            "version": 119,
            "time": {
                "meter": [{"index": 0, "numerator": 4, "denominator": 4}],
                "tempo": [{"position": 0, "bpm": float(config.bpm)}],
            },
            "library": [library_group],
            "tracks": [
                {
                    "name":          title,
                    "dispColor":     "ff7db235",
                    "dispOrder":     0,
                    "renderEnabled": False,
                    "mixer": {
                        "gainDecibel": 0,
                        "pan":         0,
                        "mute":        False,
                        "solo":        False,
                        "display":     True,
                    },
                    "mainGroup": main_group,
                    "mainRef":   self._group_ref(main_uuid),
                    "groups":    [self._group_ref(group_uuid)],
                }
            ],
            "renderConfig": {
                "destination":     "./",
                "filename":        title,
                "numChannels":     1,
                "aspirationFormat":"noAspiration",
                "bitDepth":        16,
                "sampleRate":      48000,
                "exportMixDown":   True,
            },
        }

        return json.dumps(project, ensure_ascii=False, indent=2)

    # ----------------------------
    # SVP 多音轨生成（对话文本框批量处理功能专用）
    # ----------------------------
    _MULTITRACK_DISP_COLORS = [
        "ff7db235", "ff2a7de1", "ffe0703a", "ff9b59b6",
        "ff16a085", "ffe74c3c", "fff39c12", "ff2c3e50",
        "ff1abc9c", "ffc0392b",
    ]

    def build_svp_project_text_multitrack(
        self,
        project_title: str,
        tracks: List[Dict],
        config: AudioProcessingConfig,
        word_phoneme_map: bool = False,
        language: str = "",
        dict_source: str = "default",
    ) -> str:
        """
        生成包含多条音轨的 SVP 工程文件（对话文本框批量处理功能）：
        每个对话框对应一条独立音轨，全部写入*同一个* .svp 工程文件，
        而不是分别导出多个独立工程文件。

        Parameters
        ----------
        project_title : 工程文件标题（写入 renderConfig.filename）
        tracks : 每个元素描述一条音轨：
            {
                "title": str,                            # 音轨名称（对话框文本/文件名）
                "segments": List[LabelSegment],           # 该音轨的 LAB 段落（已按需应用音素转换）
                "f0": Optional[np.ndarray],
                "t": Optional[np.ndarray],
                "native_english_words": Optional[set],    # 该行原文预提取的英语单词集合
                "midi_notes": Optional[List],             # 该音轨若导入了 MIDI 则提供，
                                                            # 用于按 MIDI 原始音高设置 tone
            }
        config : 全局共享的音频处理配置（bpm / base_pitch / f0 参数等，
                  对话文本框页面顶部的"高级设置"对全部音轨统一生效）
        word_phoneme_map / language / dict_source : 全局共享的单词→音素映射设置
            （与单文件处理页面语义一致，参见 _build_svp_track_payload）

        Returns
        -------
        str : SVP JSON 文本，library / tracks 数组长度均等于 len(tracks)
        """
        import uuid

        library: List[Dict] = []
        track_entries: List[Dict] = []

        for idx, tr in enumerate(tracks):
            tr_title = tr.get("title") or f"Track {idx + 1}"

            all_notes, pitch_data = self._build_svp_track_payload(
                segments=tr.get("segments") or [],
                f0=tr.get("f0"),
                t=tr.get("t"),
                config=config,
                midi_notes=tr.get("midi_notes"),
                word_phoneme_map=word_phoneme_map,
                language=language,
                native_english_words=tr.get("native_english_words"),
                dict_source=dict_source,
            )

            group_uuid = str(uuid.uuid4()).lower()
            main_uuid  = str(uuid.uuid4()).lower()

            library.append({
                "name": tr_title,
                "uuid": group_uuid,
                "parameters": {
                    "pitchDelta": {"mode": "cubic", "points": pitch_data},
                    "vibratoEnv":  {"mode": "cubic", "points": []},
                    "loudness":    {"mode": "cubic", "points": []},
                    "tension":     {"mode": "cubic", "points": []},
                    "breathiness": {"mode": "cubic", "points": []},
                    "voicing":     {"mode": "cubic", "points": []},
                    "gender":      {"mode": "cubic", "points": []},
                    "toneShift":   {"mode": "cubic", "points": []},
                },
                "notes": all_notes,
            })

            main_group = {
                "name":       "main",
                "uuid":       main_uuid,
                "parameters": self._empty_svp_parameters(),
                "notes":      [],
            }

            disp_color = self._MULTITRACK_DISP_COLORS[idx % len(self._MULTITRACK_DISP_COLORS)]

            track_entries.append({
                "name":          tr_title,
                "dispColor":     disp_color,
                "dispOrder":     idx,
                "renderEnabled": False,
                "mixer": {
                    "gainDecibel": 0,
                    "pan":         0,
                    "mute":        False,
                    "solo":        False,
                    "display":     True,
                },
                "mainGroup": main_group,
                "mainRef":   self._group_ref(main_uuid),
                "groups":    [self._group_ref(group_uuid)],
            })

        project = {
            "version": 119,
            "time": {
                "meter": [{"index": 0, "numerator": 4, "denominator": 4}],
                "tempo": [{"position": 0, "bpm": float(config.bpm)}],
            },
            "library": library,
            "tracks": track_entries,
            "renderConfig": {
                "destination":     "./",
                "filename":        project_title,
                "numChannels":     1,
                "aspirationFormat":"noAspiration",
                "bitDepth":        16,
                "sampleRate":      48000,
                "exportMixDown":   True,
            },
        }

        return json.dumps(project, ensure_ascii=False, indent=2)

    def _build_svp_project_text_sequenced(
        self,
        project_title: str,
        resolved_tracks: List[Dict],
        config: AudioProcessingConfig,
        word_phoneme_map: bool = False,
        language: str = "",
        dict_source: str = "default",
    ) -> str:
        """
        对话文本框批量处理功能的 SVP 生成入口（"独立序列 + 时间轴定位"模式）：
        每个对话框对应 library 里一个独立的 group（notes / pitchDelta 均使用
        该对话框自身的局部时间，不做整体平移），全部挂在*同一条*音轨的
        groups[] 列表下，各自通过 blickOffset 定位到时间轴上的位置——而不是
        像旧版 build_multitrack_project 那样把全部对话框的音符拍平合并进
        同一个 group 的 notes 列表（整体平移后再拼接）。

        这样每个对话框在 SV 编辑器里仍是一段可独立拖动/编辑的"序列"
        （group/clip），只是排列在同一条时间轴上，与 build_multitrack_project
        原有的"背靠背"累计定位规则完全一致：
            groups[i].blickOffset = 前面所有对话框真实 wav 时长之和
                                     （换算成 blick），即
                                     resolved_tracks[i]["offset_sec"] × blicks/秒。

        Parameters
        ----------
        resolved_tracks : 每个元素为 build_multitrack_project 里已解析好的
            单个对话框数据：
            {
                "title": str,
                "segments": List[LabelSegment],   # 局部时间，未整体平移
                "f0": Optional[np.ndarray],       # 局部时间
                "t": Optional[np.ndarray],        # 局部时间
                "native_english_words": Optional[set],
                "midi_notes": Optional[List],     # 局部时间 (start_sec, end_sec, pitch)
                "offset_sec": float,              # 该对话框在合并时间轴上的起始秒数
            }
        config : 全局共享的音频处理配置（bpm 决定 blicks/秒 换算，需与
            time.tempo 写入的 bpm 一致，否则各 group 摆放位置会与实际
            播放时的节拍不对齐）

        Returns
        -------
        str : SVP JSON 文本；library 长度 == len(resolved_tracks)，
              tracks 长度固定为 1（单音轨、多序列）。
        """
        import uuid
        from dataclasses import replace as _dc_replace

        offset_ratio = config.bpm * 705600000 / 60   # blicks/秒，与 _build_svp_track_payload 换算公式一致

        library: List[Dict] = []
        group_refs: List[Dict] = []

        for tr in resolved_tracks:
            tr_title = tr.get("title") or ""

            # ── 该音轨自己的有效设置：build_multitrack_project 已经把
            # "单独设置"覆盖值（或回退后的全局值）放进了 tr 里的
            # base_pitch/refine_pitch/word_phoneme_map/language/dict_source
            # 字段；未覆盖时这些值与外层的 config/word_phoneme_map/language/
            # dict_source 完全一致。bpm 恒定使用全局 config.bpm（决定整批
            # 对话框合并后的时间轴换算，不接受按音轨覆盖），因此这里只替换
            # base_pitch / refine_pitch，不改 bpm。──────────────────────
            tr_config = _dc_replace(
                config,
                base_pitch=tr.get("base_pitch", config.base_pitch),
                refine_pitch=tr.get("refine_pitch", config.refine_pitch),
            )
            tr_word_phoneme_map = tr.get("word_phoneme_map", word_phoneme_map)
            tr_language = tr.get("language", language)
            tr_dict_source = tr.get("dict_source", dict_source)

            all_notes, pitch_data = self._build_svp_track_payload(
                segments=tr.get("segments") or [],
                f0=tr.get("f0"),
                t=tr.get("t"),
                config=tr_config,
                midi_notes=tr.get("midi_notes"),
                word_phoneme_map=tr_word_phoneme_map,
                language=tr_language,
                native_english_words=tr.get("native_english_words"),
                dict_source=tr_dict_source,
            )

            group_uuid = str(uuid.uuid4()).lower()
            library.append({
                "name": tr_title or group_uuid,
                "uuid": group_uuid,
                "parameters": {
                    "pitchDelta": {"mode": "cubic", "points": pitch_data},
                    "vibratoEnv":  {"mode": "cubic", "points": []},
                    "loudness":    {"mode": "cubic", "points": []},
                    "tension":     {"mode": "cubic", "points": []},
                    "breathiness": {"mode": "cubic", "points": []},
                    "voicing":     {"mode": "cubic", "points": []},
                    "gender":      {"mode": "cubic", "points": []},
                    "toneShift":   {"mode": "cubic", "points": []},
                },
                "notes": all_notes,
            })

            ref = self._group_ref(group_uuid)
            ref["blickOffset"] = int(round(float(tr.get("offset_sec") or 0.0) * offset_ratio))
            group_refs.append(ref)

        main_uuid = str(uuid.uuid4()).lower()
        main_group = {
            "name":       "main",
            "uuid":       main_uuid,
            "parameters": self._empty_svp_parameters(),
            "notes":      [],
        }

        project = {
            "version": 119,
            "time": {
                "meter": [{"index": 0, "numerator": 4, "denominator": 4}],
                "tempo": [{"position": 0, "bpm": float(config.bpm)}],
            },
            "library": library,
            "tracks": [
                {
                    "name":          project_title,
                    "dispColor":     "ff7db235",
                    "dispOrder":     0,
                    "renderEnabled": False,
                    "mixer": {
                        "gainDecibel": 0,
                        "pan":         0,
                        "mute":        False,
                        "solo":        False,
                        "display":     True,
                    },
                    "mainGroup": main_group,
                    "mainRef":   self._group_ref(main_uuid),
                    "groups":    group_refs,
                }
            ],
            "renderConfig": {
                "destination":     "./",
                "filename":        project_title,
                "numChannels":     1,
                "aspirationFormat":"noAspiration",
                "bitDepth":        16,
                "sampleRate":      48000,
                "exportMixDown":   True,
            },
        }

        return json.dumps(project, ensure_ascii=False, indent=2)

    # ----------------------------
    # USTX 生成（修复版）
    # ----------------------------
    def _build_ustx_track_payload(
        self,
        segments: List[LabelSegment],
        f0: Optional[np.ndarray],
        t: Optional[np.ndarray],
        config: AudioProcessingConfig,
        midi_notes: Optional[List] = None,   # MIDI 导入：(start_sec, end_sec, pitch) 列表
    ) -> Tuple[List[Dict], List[Dict], int]:
        """
        构建单条 USTX 音轨所需的 notes / curves(pitd) / voice_duration。

        从原 _build_utau_project_text 中抽取，供单音轨
        (_build_utau_project_text) 与多音轨 (build_utau_project_text_multitrack，
        对话文本框批量处理功能) 共用，避免逻辑分叉导致行为不一致
        （与 _build_svp_track_payload / _build_vsqx_track_block 的抽取模式一致）。

        修复说明
        ────────
        Bug 1 — 字段错误：F0 数据原先写入每个 note 的 pitch.data（该字段
                是 OpenUtau 用于音符间过渡曲线的内部字段，不是音高偏移曲线），
                导致 PITD 曲线在 OpenUtau 中始终为空。
                正确做法：写入 voice_part["curves"] 列表，abbr = "pitd"。

        Bug 2 — 单位错误：原代码用 (midi_f0 - tone) × 1000，但 OpenUtau
                pitd 的单位是 cents（100 = 1 个半音），应为 × 100。
                原来的 × 1000 让所有偏差放大 10 倍，音高曲线完全错误。

        音符音高说明
        ────────────
        · midi_notes 提供时：直接从 MIDI 文件读取该段的音符音高（优先级最高）。
        · refine_pitch=False：所有音符固定在 base_pitch，pitd 曲线存储
          相对于 base_pitch 的完整 F0 偏移（偏差较大，但保留真实音高走势）。
        · refine_pitch=True ：每个音符的 tone 设置为该段 F0 的中位数半音，
          pitd 曲线存储相对于该 tone 的细粒度偏移（与 SVP 的行为一致）。

        Returns
        -------
        (notes, curves, voice_duration)
        """
        resolution    = 480
        ticks_per_sec = (float(config.bpm) / 60.0) * resolution

        # ── 工具：默认 pitch 过渡曲线（两端锚点，不携带 F0 数据） ───────────
        def make_default_pitch():
            return {
                "data": [
                    {"x": -1, "y": 0, "shape": "io"},
                    {"x":  1, "y": 0, "shape": "io"},
                ],
                "snap_first": True,
            }

        def make_default_vibrato():
            return {
                "length": 0, "period": 175, "depth": 25,
                "in": 10, "out": 10, "shift": 0, "drift": 0,
            }

        # ── 1. 音符收集 ──────────────────────────────────────────────────────
        notes = []
        for seg in segments:
            if seg.end_time <= seg.start_time:
                continue
            label = seg.label.strip()
            if not label or self._is_true_silence(label):
                continue

            start_sec = self._lab_time_to_seconds(seg.start_time)
            end_sec   = self._lab_time_to_seconds(seg.end_time)
            if end_sec <= start_sec:
                continue

            pos      = int(round(start_sec * ticks_per_sec))
            end_pos  = int(round(end_sec   * ticks_per_sec))
            dur_tick = max(1, end_pos - pos)

            tone = int(config.base_pitch)
            _seg_has_midi = midi_notes is not None and self._midi_notes_overlap_segment(
                start_sec, end_sec, midi_notes
            )
            if _seg_has_midi:
                # ── MIDI 模式：直接从 MIDI 文件读取该段的音符音高 ──
                # （仅当该段时间范围内确有 MIDI 音符重叠时才采用；顺序合并
                # 多个对话框时，没有 MIDI 的对话框段落不会被其它对话框的
                # midi_notes 误判命中，见 _midi_notes_overlap_segment）
                from midi_processor import map_segment_to_midi_pitch
                tone = map_segment_to_midi_pitch(start_sec, end_sec, midi_notes,
                                                 base_pitch=int(config.base_pitch))
            elif config.refine_pitch and f0 is not None and t is not None:
                # refine_pitch：用该段 F0 中位数半音作为音符音高
                mask   = (t >= start_sec) & (t < end_sec)
                seg_f0 = f0[mask]
                voiced = seg_f0[seg_f0 > 0]
                if len(voiced) > 0:
                    midi_vals = 69.0 + 12.0 * np.log2(voiced / 440.0)
                    tone      = int(round(float(np.median(midi_vals))))
                    tone      = max(0, min(127, tone))

            # ── OpenUTAU lyric 规范化 ───────────────────────────────────
            # OpenUTAU 使用 "+" 作为延音符（音符延续/Tie）：
            #   "ー"（U+30FC 長音符）→ "+"：日语长音在 USTX 中应写作 "+"
            #   "-"（辅音起始占位符）→ "+"：OpenUTAU 用 "+" 表示 consonant tie
            # 此替换仅影响 USTX 输出，SVP/VSQX 格式保持原有语义。
            utau_lyric = "+" if label in ("ー", "-") else label

            notes.append({
                "position": pos,
                "duration": dur_tick,
                "tone":     tone,
                "lyric":    utau_lyric,
                # pitch.data = 默认两端锚点，不存 F0（F0 走 voice_part curves）
                "pitch":    make_default_pitch(),
                "vibrato":  make_default_vibrato(),
                "phoneme_expressions": [],
                "phoneme_overrides":   [],
            })

        voice_duration = max(
            (n["position"] + n["duration"] for n in notes),
            default=0,
        )

        # ── 2. 构建全局 pitd 曲线 ────────────────────────────────────────────
        # OpenUtau pitd 单位：cents（100 = 1 个半音），范围 -1200 ~ +1200。
        # 数据放在 voice_part["curves"] 而非 note["pitch"]["data"]。
        xs: List[int] = []
        ys: List[int] = []

        if config.export_pitch_line and f0 is not None and t is not None and len(f0) > 0:
            # 预建 note tone 查询表：(start_tick, end_tick, tone) 按 start 排序
            note_ranges = np.array(
                [(n["position"], n["position"] + n["duration"], n["tone"])
                 for n in notes],
                dtype=np.int64,
            ) if notes else np.empty((0, 3), dtype=np.int64)

            note_starts = note_ranges[:, 0] if len(note_ranges) else np.array([], dtype=np.int64)
            note_ends   = note_ranges[:, 1] if len(note_ranges) else np.array([], dtype=np.int64)
            note_tones  = note_ranges[:, 2] if len(note_ranges) else np.array([], dtype=np.int64)

            for ti, f0i in zip(t, f0):
                if f0i <= 0.0:
                    continue
                tick    = int(round(float(ti) * ticks_per_sec))
                midi_f0 = 69.0 + 12.0 * np.log2(float(f0i) / 440.0)

                # 找该 tick 所属音符的 tone（二分查找，O(log n)）
                nominal = int(config.base_pitch)
                if len(note_starts):
                    idx = int(np.searchsorted(note_starts, tick, side="right")) - 1
                    if 0 <= idx < len(note_tones) and tick <= note_ends[idx]:
                        nominal = int(note_tones[idx])

                # ★ 单位修复：× 100（cents），不是 × 1000
                deviation = int(round((midi_f0 - nominal) * 100))
                xs.append(tick)
                ys.append(deviation)

        # ★ 字段修复：pitd 曲线放在 voice_part["curves"]，而非 note["pitch"]["data"]
        curves = [{"xs": xs, "ys": ys, "abbr": "pitd"}] if xs else []

        return notes, curves, voice_duration

    def _build_utau_project_text(
        self,
        title: str,
        segments: List[LabelSegment],
        f0: Optional[np.ndarray],
        t: Optional[np.ndarray],
        sr: Optional[int],
        wav_path: str,
        config: AudioProcessingConfig,
        audio_duration_sec: Optional[float] = None,
        midi_notes: Optional[List] = None,   # MIDI 导入：(start_sec, end_sec, pitch) 列表
    ) -> str:
        """生成单音轨 USTX 工程文件内容（音符/曲线计算见 _build_ustx_track_payload）。"""
        from ruamel.yaml import YAML
        from io import StringIO

        resolution = 480

        notes, curves, voice_duration = self._build_ustx_track_payload(
            segments=segments, f0=f0, t=t, config=config, midi_notes=midi_notes,
        )

        # ── Track + VoicePart ─────────────────────────────────────────────
        track = {
            "phonemizer":        "OpenUtau.Core.DefaultPhonemizer",
            "renderer_settings": {},
            "mute":   False,
            "solo":   False,
            "volume": 0,
        }

        voice_part = {
            "name":     title,
            "comment":  "",
            "duration": voice_duration,
            "track_no": 0,
            "position": 0,
            "notes":    notes,
            "curves":   curves,   # ← pitd 写这里
        }

        # ── 顶层项目结构 ──────────────────────────────────────────────────
        project_obj = {
            "name":        title,
            "comment":     "",
            "output_dir":  "Vocal",
            "cache_dir":   "UCache",
            "ustx_version": "0.6",
            "resolution":  resolution,
            "bpm":         float(config.bpm),
            "beat_per_bar": 4,
            "beat_unit":    4,
            "time_signatures": [{"bar_position": 0, "beat_per_bar": 4, "beat_unit": 4}],
            "tempos":          [{"position": 0, "bpm": float(config.bpm)}],
            "expressions":      self._get_default_expressions(),
            "tracks":           [track],
            "voice_parts":      [voice_part],
            "wave_parts":       [],
        }

        yaml = YAML()
        yaml.default_flow_style               = False
        yaml.allow_unicode                    = True
        yaml.sort_base_mapping_type_on_output = False

        stream = StringIO()
        yaml.dump(project_obj, stream)
        return stream.getvalue()

    def build_utau_project_text_multitrack(
        self,
        project_title: str,
        tracks: List[Dict],
        config: AudioProcessingConfig,
    ) -> str:
        """
        生成包含多条音轨的 USTX 工程文件（对话文本框批量处理功能）：
        每个对话框对应一条独立音轨（独立 track + voice_part），全部写入
        *同一个* .ustx 工程文件，与 build_svp_project_text_multitrack /
        build_vsqx_project_text_multitrack 语义一致。

        USTX 原生支持多 track_no + 多 voice_part（每个 voice_part 通过
        track_no 关联到 tracks 列表中的一条音轨），不需要像 SVP 的
        library/mainGroup 那样做额外的分组包装。

        Parameters
        ----------
        project_title : 工程文件标题（写入顶层 name 字段）
        tracks : 每个元素描述一条音轨：
            {
                "title": str,                          # 音轨/voice_part 名称
                "segments": List[LabelSegment],         # 该音轨的 LAB 段落
                "f0": Optional[np.ndarray],
                "t": Optional[np.ndarray],
                "midi_notes": Optional[List],           # 该音轨若来自 MIDI 导入则提供，
                                                          # 用于按 MIDI 原始音高设置 tone
            }
            （USTX 不支持词典/单词→音素映射，故不需要 native_english_words /
             word_phoneme_map / dict_source 等字段——与 showDictSource /
             showWordPhonemeMap 在输出格式为 ustx 时隐藏这些前端控件的语义一致）
        config : 全局共享的音频处理配置（bpm / base_pitch / f0 参数等）

        Returns
        -------
        str : USTX（YAML）文本，tracks / voice_parts 数组长度均等于 len(tracks)
        """
        from ruamel.yaml import YAML
        from io import StringIO

        resolution = 480

        ustx_tracks: List[Dict] = []
        voice_parts: List[Dict] = []

        for idx, tr in enumerate(tracks):
            tr_title = tr.get("title") or f"Track {idx + 1}"

            notes, curves, voice_duration = self._build_ustx_track_payload(
                segments=tr.get("segments") or [],
                f0=tr.get("f0"),
                t=tr.get("t"),
                config=config,
                midi_notes=tr.get("midi_notes"),
            )

            ustx_tracks.append({
                "phonemizer":        "OpenUtau.Core.DefaultPhonemizer",
                "renderer_settings": {},
                "mute":   False,
                "solo":   False,
                "volume": 0,
            })

            voice_parts.append({
                "name":     tr_title,
                "comment":  "",
                "duration": voice_duration,
                "track_no": idx,
                "position": 0,
                "notes":    notes,
                "curves":   curves,
            })

        project_obj = {
            "name":        project_title,
            "comment":     "",
            "output_dir":  "Vocal",
            "cache_dir":   "UCache",
            "ustx_version": "0.6",
            "resolution":  resolution,
            "bpm":         float(config.bpm),
            "beat_per_bar": 4,
            "beat_unit":    4,
            "time_signatures": [{"bar_position": 0, "beat_per_bar": 4, "beat_unit": 4}],
            "tempos":          [{"position": 0, "bpm": float(config.bpm)}],
            "expressions":      self._get_default_expressions(),
            "tracks":           ustx_tracks,
            "voice_parts":      voice_parts,
            "wave_parts":       [],
        }

        yaml = YAML()
        yaml.default_flow_style               = False
        yaml.allow_unicode                    = True
        yaml.sort_base_mapping_type_on_output = False

        stream = StringIO()
        yaml.dump(project_obj, stream)
        return stream.getvalue()

    def _build_ustx_project_text_sequenced(
        self,
        project_title: str,
        resolved_tracks: List[Dict],
        config: AudioProcessingConfig,
    ) -> str:
        """
        对话文本框批量处理功能的 USTX 生成入口（"独立序列 + 时间轴定位"模式）：
        每个对话框对应一个独立的 voice_part（notes / curves 均使用该对话框
        自身的局部时间，不做整体平移），全部挂在*同一条* track_no=0 下，
        各自通过 voice_part["position"]（tick）定位到时间轴上的位置——而不是
        像旧版 build_multitrack_project 那样把全部对话框的音符拍平合并进
        同一个 voice_part 的 notes 列表。

        OpenUtau 原生支持一条 track_no 下挂多个 voice_part（每个 part 都是
        可独立拖动的片段，part 内 note.position 相对 part 自身的 position
        为 0），因此这里不需要像 build_utau_project_text_multitrack 那样
        为每个对话框分配独立的 track_no（那是旧版"背靠背多音轨"的做法）。

        Parameters
        ----------
        resolved_tracks : 每个元素为 build_multitrack_project 里已解析好的
            单个对话框数据（含局部时间的 segments/f0/t/midi_notes），额外
            需要 "offset_sec" 字段（该对话框在合并时间轴上的起始秒数，
            用于换算成 voice_part["position"]）。

        Returns
        -------
        str : USTX（YAML）文本；tracks 长度固定为 1，
              voice_parts 长度 == len(resolved_tracks)。
        """
        from ruamel.yaml import YAML
        from io import StringIO

        resolution = 480
        ticks_per_sec = (float(config.bpm) / 60.0) * resolution

        voice_parts: List[Dict] = []
        for tr in resolved_tracks:
            notes, curves, voice_duration = self._build_ustx_track_payload(
                segments=tr.get("segments") or [],
                f0=tr.get("f0"),
                t=tr.get("t"),
                config=config,
                midi_notes=tr.get("midi_notes"),
            )

            position = int(round(float(tr.get("offset_sec") or 0.0) * ticks_per_sec))
            voice_parts.append({
                "name":     tr.get("title") or "",
                "comment":  "",
                "duration": voice_duration,
                "track_no": 0,
                "position": position,
                "notes":    notes,
                "curves":   curves,
            })

        track = {
            "phonemizer":        "OpenUtau.Core.DefaultPhonemizer",
            "renderer_settings": {},
            "mute":   False,
            "solo":   False,
            "volume": 0,
        }

        project_obj = {
            "name":        project_title,
            "comment":     "",
            "output_dir":  "Vocal",
            "cache_dir":   "UCache",
            "ustx_version": "0.6",
            "resolution":  resolution,
            "bpm":         float(config.bpm),
            "beat_per_bar": 4,
            "beat_unit":    4,
            "time_signatures": [{"bar_position": 0, "beat_per_bar": 4, "beat_unit": 4}],
            "tempos":          [{"position": 0, "bpm": float(config.bpm)}],
            "expressions":      self._get_default_expressions(),
            "tracks":           [track],
            "voice_parts":      voice_parts,
            "wave_parts":       [],
        }

        yaml = YAML()
        yaml.default_flow_style               = False
        yaml.allow_unicode                    = True
        yaml.sort_base_mapping_type_on_output = False

        stream = StringIO()
        yaml.dump(project_obj, stream)
        return stream.getvalue()

    def _get_default_expressions(self) -> Dict:
            """补齐了样本文件中定义的所有弯曲控制和核心包络表达式"""
            return {
                "dyn":  {"name": "dynamics (curve)",        "abbr": "dyn",  "type": "Curve",     "min": -240,  "max": 120,  "default_value": 0,   "is_flag": False, "flag": ""},
                "pitd": {"name": "pitch deviation (curve)", "abbr": "pitd", "type": "Curve",     "min": -1200, "max": 1200, "default_value": 0,   "is_flag": False, "flag": ""},
                "clr":  {"name": "voice color",             "abbr": "clr",  "type": "Options",   "min": 0,     "max": -1,   "default_value": 0,   "is_flag": False, "options": []},
                "eng":  {"name": "resampler engine",        "abbr": "eng",  "type": "Options",   "min": 0,     "max": 1,    "default_value": 0,   "is_flag": False, "options": ["", "worldline"]},
                "vel":  {"name": "velocity",                "abbr": "vel",  "type": "Numerical", "min": 0,     "max": 200,  "default_value": 100, "is_flag": False, "flag": ""},
                "vol":  {"name": "volume",                  "abbr": "vol",  "type": "Numerical", "min": 0,     "max": 200,  "default_value": 100, "is_flag": False, "flag": ""},
                "atk":  {"name": "attack",                  "abbr": "atk",  "type": "Numerical", "min": 0,     "max": 200,  "default_value": 100, "is_flag": False, "flag": ""},
                "dec":  {"name": "decay",                   "abbr": "dec",  "type": "Numerical", "min": 0,     "max": 100,  "default_value": 0,   "is_flag": False, "flag": ""},
                "gen":  {"name": "gender",                  "abbr": "gen",  "type": "Numerical", "min": -100,  "max": 100,  "default_value": 0,   "is_flag": True,  "flag": "g"},
                "genc": {"name": "gender (curve)",          "abbr": "genc", "type": "Curve",     "min": -100,  "max": 100,  "default_value": 0,   "is_flag": False, "flag": ""},
                "bre":  {"name": "breath",                  "abbr": "bre",  "type": "Numerical", "min": 0,     "max": 100,  "default_value": 0,   "is_flag": True,  "flag": "B"},
                "brec": {"name": "breathiness (curve)",     "abbr": "brec", "type": "Curve",     "min": -100,  "max": 100,  "default_value": 0,   "is_flag": False, "flag": ""},
                "lpf":  {"name": "lowpass",                 "abbr": "lpf",  "type": "Numerical", "min": 0,     "max": 100,  "default_value": 0,   "is_flag": True,  "flag": "H"},
                "mod":  {"name": "modulation",              "abbr": "mod",  "type": "Numerical", "min": 0,     "max": 100,  "default_value": 0,   "is_flag": False, "flag": ""},
                "alt":  {"name": "alternate",               "abbr": "alt",  "type": "Numerical", "min": 0,     "max": 16,   "default_value": 0,   "is_flag": False, "flag": ""},
                "shft": {"name": "tone shift",              "abbr": "shft", "type": "Numerical", "min": -36,   "max": 36,   "default_value": 0,   "is_flag": False, "flag": ""},
                "shfc": {"name": "tone shift (curve)",      "abbr": "shfc", "type": "Curve",     "min": -1200, "max": 1200, "default_value": 0,   "is_flag": False, "flag": ""},
                "tenc": {"name": "tension (curve)",         "abbr": "tenc", "type": "Curve",     "min": -100,  "max": 100,  "default_value": 0,   "is_flag": False, "flag": ""},
                "voic": {"name": "voicing (curve)",         "abbr": "voic", "type": "Curve",     "min": 0,     "max": 100,  "default_value": 100, "is_flag": False, "flag": ""},
            }

    # ----------------------------
    # 完整流程入口
    # ----------------------------
    def process_full_pipeline(
        self,
        wav_path: str,
        lab_path: Optional[str] = None,   # 可选：提供 LAB 或 MIDI 之一即可
        output_format: str = "sv",
        project_title: str = "Project",
        config: Optional[AudioProcessingConfig] = None,
        audio_f0_data: Optional[Dict] = None,
        phoneme_mode: str = "none",
        midi_path: Optional[str] = None,   # MIDI 文件路径（可选）
        lyrics_text: str = "",             # 纯 MIDI 模式下的用户歌词原文
        vsqx_singer: str = "MIKU_V4_Chinese",       # VSQX 歌手名（由前端按语种/模式传入）
        vsqx_singer_id: str = "BNGE7CP7EMTRSNC3",  # VSQX 歌手 ID
        vsqx_singer_bs: int = 4,                    # VSQX 歌手 Bank Select（VOCALOID4 内部编号，系统相关）
        word_phoneme_map: bool = False,             # 英语单词 → 音素写入 SVP phonemes / VSQX <p lock="1">
        language: str = "",                         # 语种，传给 SVP/VSQX 构建器防误判
        original_text: str = "",                    # 原始歌词文本（pypinyin 转换前的汉字/韩文原文）
                                                    # 用于预提取真实英语单词集合，防止拼音被误判
        dict_source: str = "default",               # 单词→音素词典来源："default"/"synthesizerv"/"vocaloid"
        ja_devoiced_phoneme: bool = False,              # 日语辅音起始音素锁定：仅生成工程模式下，
                                                    # phoneme_mode 为 merge/hiragana/katakana 时，
                                                    # 将合并音节的辅音起始（如 s / sh / ky）写入
                                                    # VSQX <p lock="1"> 并锁定；单独元音/ん/っ 不锁定。
    ) -> Dict:
        """完整工程文件生成入口。

        输入约束
        ────────
        • wav_path  —— 始终必须（F0 提取来源）
        • lab_path  —— 可选；提供时从 LAB 读取音素段落
        • midi_path —— 可选；提供时解析 BPM + 音符音高；
                        若同时缺少 lab_path，则从 MIDI 音符自动生成段落

        至少需要 lab_path 或 midi_path 其中一个。

        输出格式
        ────────
        sv / svp        → Synthesizer V .svp
        ustx / utau     → OpenUtau .ustx
        midi / mid      → MIDI 标准文件 .mid
        """
        try:
            config   = config or AudioProcessingConfig()
            wav_path = str(wav_path)

            # ── 预提取原始文本中的真实英语单词集合（word_phoneme_map 防误判）──
            # 必须在 pypinyin 等转换之前，从原始汉字/韩文文本提取。
            # 纯中文/韩文时集合为空，所有 label 均不会被当作英语词处理；
            # 混入真实英语时（如 "I love you 很多"），只有这些词命中。
            native_english_words: Optional[set] = None
            _lang_norm = (language or "").lower().strip().rstrip("-")
            if word_phoneme_map and _lang_norm not in ("en", "eng", "english") and original_text:
                try:
                    from phoneme_converter import extract_native_english_words
                    native_english_words = extract_native_english_words(original_text)
                    logger.info(
                        "[word_phoneme_map] 原始文本预提取英语单词 %d 个: %s",
                        len(native_english_words),
                        sorted(native_english_words)[:10] if native_english_words else "（无）",
                    )
                except Exception as _nee_err:
                    logger.warning("[word_phoneme_map] 预提取英语单词失败: %s，回退词典查询", _nee_err)
                    # native_english_words 保持 None → 兼容旧行为

            # ── 文件检查 ───────────────────────────────────────────────────────
            if not Path(wav_path).exists():
                return {"success": False, "error": f"WAV 文件不存在: {wav_path}"}

            lab_exists  = bool(lab_path  and Path(str(lab_path)).exists())
            midi_exists = bool(midi_path and Path(str(midi_path)).exists())

            if not lab_exists and not midi_exists:
                return {
                    "success": False,
                    "error": "需要 LAB 文件或 MIDI 文件（至少提供其中一个）",
                }

            audio_duration_sec = self._read_audio_duration_sec(wav_path)

            # ── ① MIDI 优先解析（BPM + 音符，供后续用） ───────────────────────
            midi_notes = None
            midi_lyric_words = []

            if midi_exists:
                try:
                    from dataclasses import replace as _dc_replace
                    from midi_processor import parse_midi_notes_with_lyrics

                    midi_bpm, midi_notes, midi_lyrics = parse_midi_notes_with_lyrics(midi_path)

                    if midi_bpm and midi_bpm > 0:
                        config = _dc_replace(config, bpm=float(midi_bpm))
                        logger.info(f"✓ MIDI BPM 已覆盖 config.bpm → {midi_bpm:.1f}")

                    logger.info(f"✓ MIDI 音符导入: {len(midi_notes)} 个")

                    midi_lyric_words = self._midi_lyrics_to_words(midi_lyrics)
                    logger.info(f"✓ MIDI 歌词导入: {len(midi_lyric_words)} 个词")

                except Exception as _midi_err:
                    logger.warning(f"⚠ MIDI 解析失败，回退到默认模式: {_midi_err}")
                    midi_notes = None
                    midi_lyric_words = []

            # ── ② 段落来源：优先 LAB，否则从 MIDI 音符生成 ──────────────────
            if lab_exists:
                segments = self._load_lab_segments(str(lab_path))

                # 如果 MIDI 里有歌词，就覆盖 LAB 的 label
                if midi_lyric_words:
                    segments = self._apply_midi_lyrics_to_segments(segments, midi_lyric_words)

                if phoneme_mode and phoneme_mode != "none":
                    try:
                        from phoneme_converter import apply_phoneme_mode
                        seg_tuples = [(s.start_time, s.end_time, s.label) for s in segments]
                        # ja_devoiced_phoneme 只在"仅生成工程"场景下由前端传入，
                        # 要求 apply_phoneme_mode 一并返回每个合并音节的辅音
                        # 起始音素（用于下游 VSQX <p lock="1"> / SVP phonemes 写入）。
                        # devoiced_target 必须按 output_format 区分：sv/svp 用
                        # SynthesizerV 罗马字体系（辅音原样保留），其余（vsqx）
                        # 用 VOCALOID4 官方去母音化专属符号（k'/C/p'/S/tS/p\），
                        # 否则 SVP 会收到它不认识的 VOCALOID4 专属符号。
                        _devoiced_target = "synthesizerv" if output_format in ("sv", "svp") else "vocaloid4"
                        converted = apply_phoneme_mode(
                            seg_tuples, phoneme_mode,
                            with_devoiced_phoneme=ja_devoiced_phoneme,
                            devoiced_target=_devoiced_target,
                        )
                        if ja_devoiced_phoneme:
                            segments = [
                                LabelSegment(
                                    start_time=t[0], end_time=t[1], label=t[2],
                                    devoiced_phoneme=t[3],
                                )
                                for t in converted
                            ]
                        else:
                            segments = [
                                LabelSegment(start_time=t[0], end_time=t[1], label=t[2])
                                for t in converted
                            ]
                        logger.info(f"音素转换完成 (mode={phoneme_mode}, ja_devoiced_phoneme={ja_devoiced_phoneme}): {len(segments)} 个音节段落")
                    except Exception as _pm_err:
                        logger.warning(f"音素转换失败 (mode={phoneme_mode}): {_pm_err}，回退到原始音素")
            else:
                if not midi_notes:
                    return {"success": False, "error": "MIDI 文件解析失败，无法生成段落"}

                # 纯 MIDI 模式：优先用 MIDI 自带歌词；没有的话再用外部 lyrics_text
                lyric_words = midi_lyric_words
                if not lyric_words and lyrics_text and lyrics_text.strip():
                    lyric_words = _split_lyrics_to_words(lyrics_text)

                segments = self._segments_from_midi_notes(
                    midi_notes,
                    lyric_words=lyric_words
                )

                logger.info(
                    f"✓ 从 MIDI 生成段落: {len(segments)} 个"
                    f"（歌词字数: {len(lyric_words) if lyric_words else 0}）"
                )

            # ── ③ F0 数据 ─────────────────────────────────────────────────────
            f0, t, sr = None, None, None
            if audio_f0_data and audio_f0_data.get("success"):
                f0 = audio_f0_data.get("f0")
                t  = audio_f0_data.get("t")
                sr = audio_f0_data.get("sr")

            # ── ④ 生成输出文件 ───────────────────────────────────────────────
            fmt = self._normalize_output_format(output_format)

            if fmt == "sv":
                project_text = self._build_svp_project_text(
                    title=project_title, segments=segments,
                    f0=f0, t=t, sr=sr, wav_path=wav_path,
                    config=config, audio_duration_sec=audio_duration_sec,
                    midi_notes=midi_notes,
                    word_phoneme_map=word_phoneme_map,
                    language=language,
                    native_english_words=native_english_words,
                    dict_source=dict_source,
                )
                out_path = self.work_dir / f"{Path(wav_path).stem}.svp"
                out_path.write_text(project_text, encoding="utf-8")

            elif fmt == "ustx":
                project_text = self._build_utau_project_text(
                    title=project_title, segments=segments,
                    f0=f0, t=t, sr=sr, wav_path=wav_path,
                    config=config, audio_duration_sec=audio_duration_sec,
                    midi_notes=midi_notes,
                )
                out_path = self.work_dir / f"{Path(wav_path).stem}.ustx"
                out_path.write_text(project_text, encoding="utf-8")

            elif fmt == "midi":
                out_path = self.work_dir / f"{Path(wav_path).stem}.mid"
                self._build_midi_output(
                    output_path=str(out_path),
                    segments=segments,
                    config=config,
                    f0=f0, t=t,
                    midi_notes=midi_notes,
                )

            elif fmt == "vsqx":
                project_text = self._build_vsqx_project_text(
                    title=project_title, segments=segments,
                    f0=f0, t=t, sr=sr, wav_path=wav_path,
                    config=config, audio_duration_sec=audio_duration_sec,
                    midi_notes=midi_notes,
                    vsqx_singer=vsqx_singer,
                    vsqx_singer_id=vsqx_singer_id,
                    vsqx_singer_bs=vsqx_singer_bs,
                    word_phoneme_map=word_phoneme_map,
                    language=language,
                    native_english_words=native_english_words,
                    dict_source=dict_source,
                )
                out_path = self.work_dir / f"{Path(wav_path).stem}.vsqx"
                out_path.write_text(project_text, encoding="utf-8")

            else:
                return {
                    "success": False,
                    "error": (
                        f"不支持的格式: {output_format}。"
                        "支持: sv / ustx / vsqx"
                    ),
                }

            return {
                "success":     True,
                "output_path": str(out_path),
                "segments":    len(segments),
                "format":      fmt,
                "title":       project_title,
            }

        except Exception as e:
            logger.error(f"工程文件生成失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ----------------------------
    # 批量顺序合并工程文件生成（对话文本框批量处理功能专用）
    # ----------------------------
    def build_multitrack_project(
        self,
        project_title: str,
        track_inputs: List[Dict],
        output_format: str,
        config: AudioProcessingConfig,
        word_phoneme_map: bool = False,
        language: str = "",
        dict_source: str = "default",
        vsqx_singer: str = "MIKU_V4_Chinese",
        vsqx_singer_id: str = "BNGE7CP7EMTRSNC3",
        vsqx_singer_bs: int = 4,
        phoneme_mode: str = "none",
        ja_devoiced_phoneme: bool = False,   # 日语辅音起始音素锁定（仅 vsqx 生效，
                                           # 语义与 process_full_pipeline 一致）
    ) -> Dict:
        """
        对话文本框批量处理功能的工程文件生成入口：不再为每个对话框生成
        独立音轨（多音轨工程），而是把全部对话框写入*同一个*单音轨工程
        文件——但每个对话框仍是该音轨下一段独立的序列（SVP: library
        group；USTX: voice_part；VSQX: vsPart），只是通过各自的时间轴
        偏移量摆放到对应位置，*不会*把多个对话框的音符拍平合并进同一个
        序列的 notes 列表里。这样每个对话框在编辑器里仍可被单独拖动、
        编辑、静音，不会和相邻对话框的音符纠缠在一起。

        时间轴定位规则（音频推断分段起始时间）：
            第 i 个对话框所在序列的起始时间 = 排在它前面的全部对话框的
            真实音频时长（由磁盘上的 wav 文件时长决定）之和，即"上一段
            结束的瞬间就是下一段的开始"，无缝衔接、不裁剪首尾静音、也不
            与任何原始完整音频做互相关比对——每个对话框自身的 LAB / MIDI
            时间戳保持局部（相对该对话框自身为 0），只通过这个整体偏移量
            定位，不会被改写。

        Parameters
        ----------
        project_title : 工程文件标题 / 输出文件名（不含扩展名）
        track_inputs : 每个元素描述一个已完成对齐（或已导入 LAB/MIDI）的对话框：
            {
                "title": str,             # 段落标题（对话框文本前若干字符，或文件名），
                                           # 目前仅用于日志/兜底歌词来源，不再对应工程文件里的音轨名
                "wav_path": str,          # 该对话框的音频文件路径（必须存在；其真实时长
                                           # 决定下一个对话框在合并时间轴上的起始位置）
                "lab_path": str,          # 该对话框的 LAB 标注文件路径（可选，与 midi_path 二选一/优先）
                "midi_path": str,         # 该对话框的 MIDI 文件路径（可选；无 lab_path 时用于生成段落+音高）
                "original_text": str,     # 原始歌词/台词文本，用于 word_phoneme_map 防误判（可选）
                "config": AudioProcessingConfig,  # 可选：该对话框自己的有效 F0/音高配置
                                           # （"单独设置"覆盖值，或未覆盖时与下方全局 config 相同；
                                           # 仅使用其中的 base_pitch / refine_pitch，bpm 恒定使用
                                           # 全局 config.bpm，不接受按对话框覆盖）
                "language": str,          # 可选：该对话框自己的有效语言（未提供时回退到下方全局 language）
                "word_phoneme_map": bool, # 可选：该对话框自己的有效单词映射开关（回退到全局 word_phoneme_map）
                "dict_source": str,       # 可选：该对话框自己的有效词典来源（回退到全局 dict_source）
                "phoneme_mode": str,      # 可选：该对话框自己的有效音素转换模式（回退到全局 phoneme_mode）
            }
            调用方负责保证列表顺序即为对话框顺序（决定合并后时间轴上的先后位置）。
        output_format : 支持 "sv"（Synthesizer V .svp）/ "vsqx"（VOCALOID4 .vsqx）/
            "ustx"（OpenUtau .ustx）。不支持 "midi"（调用方应改用单文件流程逐个导出）。
        config : 整批对话框的默认音频处理配置（对话文本框页面顶部"高级设置"，
            未开启"单独设置"的对话框直接使用这份配置；bpm 无论是否开启"单独
            设置"都始终使用这里的全局值，决定全部对话框合并后共享的时间轴换算）
        word_phoneme_map / language / dict_source : 整批对话框的默认单词→音素
            映射设置（仅 sv / vsqx 使用；USTX 没有 phonemes 字段，调用方应确保
            输出为 ustx 时前端已隐藏这两个控件，这里也不会对 ustx 生效）。
            某个对话框在 track_inputs 里提供了同名字段时，仅该对话框使用
            覆盖值，其余对话框仍使用这里的默认值。
        vsqx_singer / vsqx_singer_id / vsqx_singer_bs : 合并后单音轨共用的 VSQX 声库
        phoneme_mode : 整批对话框默认的音素转换模式（"none" / "merge" / "hiragana" /
            "katakana"），应用于每个对话框的 LAB 段落（与单文件"仅生成工程"模式
            语义一致）。仅在段落来自 LAB 且输出格式为 sv / vsqx 时生效；USTX 始终
            使用原始音素/文字，不应用转换（与用户预期一致：USTX 侧重歌词音节而非
            音素级精修）；来自 MIDI 的段落（歌词文本）同样不应用音素转换。某个
            对话框在 track_inputs 里提供了 "phoneme_mode" 字段时，仅该对话框使用
            覆盖值。
        音高解析说明（混合 MIDI / 非 MIDI 对话框场景）：
            部分对话框提供 MIDI、部分没有时，没有 MIDI 的对话框段落仍然按
            该对话框自身的 refine_pitch / base_pitch 规则解析音高，不会被
            其它对话框的 MIDI 音符列表影响（见 _midi_notes_overlap_segment）。

        Returns
        -------
        Dict : {"success": True, "output_path": str, "format": str,
                "track_count": 1, "segment_count": int}
               或 {"success": False, "error": str}
        """
        try:
            fmt = self._normalize_output_format(output_format)
            if fmt not in ("sv", "vsqx", "ustx"):
                return {
                    "success": False,
                    "error": f"对话文本框批量处理暂不支持的输出格式: {output_format}（仅支持 sv / vsqx / ustx）",
                }

            if not track_inputs:
                return {"success": False, "error": "没有可用于生成工程文件的对话框（所有对话框均被跳过）"}

            if phoneme_mode not in ("none", "merge", "hiragana", "katakana"):
                phoneme_mode = "none"

            resolved_tracks: List[Dict] = []
            for tr in track_inputs:
                wav_path  = tr.get("wav_path")
                lab_path  = tr.get("lab_path")
                midi_path = tr.get("midi_path")
                tr_title = tr.get("title") or Path(str(wav_path)).stem
                original_text = tr.get("original_text", "") or ""

                # ── 该对话框自己的有效设置：process_dialogue_batch 已经把
                # "单独设置"覆盖值（或回退后的全局值）放进了 track_inputs
                # 里的 config/language/word_phoneme_map/dict_source/
                # phoneme_mode 字段；这里若某个字段缺失（例如调用方是尚未
                # 升级的旧代码，直接传了不含这些 key 的 track_inputs），
                # 则回退到 build_multitrack_project 自己的同名参数，与该
                # 功能上线前的行为完全一致。────────────────────────────
                tr_config = tr.get("config") or config
                tr_language = tr.get("language", language)
                tr_word_phoneme_map = tr.get("word_phoneme_map", word_phoneme_map)
                tr_dict_source = tr.get("dict_source", dict_source)
                tr_phoneme_mode = tr.get("phoneme_mode", phoneme_mode)
                if tr_phoneme_mode not in ("none", "merge", "hiragana", "katakana"):
                    tr_phoneme_mode = "none"
                tr_ja_devoiced_phoneme = bool(tr.get("ja_devoiced_phoneme", ja_devoiced_phoneme))

                if not wav_path or not Path(str(wav_path)).exists():
                    logger.warning("[多音轨] 跳过音轨 %r：WAV 文件不存在 (%s)", tr_title, wav_path)
                    continue

                lab_exists  = bool(lab_path  and Path(str(lab_path)).exists())
                midi_exists = bool(midi_path and Path(str(midi_path)).exists())

                if not lab_exists and not midi_exists:
                    logger.warning("[多音轨] 跳过音轨 %r：LAB / MIDI 均不存在", tr_title)
                    continue

                # ── 段落来源：优先 LAB，否则从 MIDI 音符自动生成 ──────────────
                # LAB 优先于 MID（与前端"lab_{i} 高优先级，其次 mid_{i}"约定一致）。
                midi_notes = None
                midi_lyrics = []
                if midi_exists:
                    try:
                        from midi_processor import parse_midi_notes_with_lyrics
                        from dataclasses import replace as _dc_replace

                        midi_bpm, midi_notes, midi_lyrics = parse_midi_notes_with_lyrics(str(midi_path))
                        if midi_bpm and midi_bpm > 0:
                            # 注意：这里只替换该音轨自己的 tr_config，不再覆盖
                            # 外层共享的 config 变量——bpm 决定整批对话框合并
                            # 后的时间轴换算，必须全局统一，不应被某一个提供
                            # 了 MIDI 的对话框悄悄改写，影响到它后面的音轨。
                            tr_config = _dc_replace(tr_config, bpm=float(midi_bpm))
                    except Exception as _midi_err:
                        logger.warning("[多音轨] 音轨 %r MIDI 解析失败: %s", tr_title, _midi_err)
                        midi_notes = None
                        midi_lyrics = []

                if lab_exists:
                    segments = self._load_lab_segments(str(lab_path))

                    midi_lyric_words = self._midi_lyrics_to_words(midi_lyrics) if midi_exists else []
                    if midi_lyric_words:
                        segments = self._apply_midi_lyrics_to_segments(segments, midi_lyric_words)

                    # 音素转换：仅对来自 LAB 的段落、且目标格式非 USTX 时生效
                    # （USTX 始终使用原始音素/文字，不应用转换——见函数 docstring）。
                    # 使用该音轨自己的 tr_phoneme_mode（"仅生成工程"模式下可
                    # 按对话框单独覆盖，未覆盖时即为整批统一的 phoneme_mode）。
                    if tr_phoneme_mode != "none" and fmt != "ustx":
                        try:
                            from phoneme_converter import apply_phoneme_mode
                            seg_tuples = [(s.start_time, s.end_time, s.label) for s in segments]
                            # 去母音化音素写入对 sv（SVP phonemes 字段）和 vsqx
                            # （<p lock="1">）都有意义；USTX 已在上面被整体排除
                            # （fmt != "ustx"），故这里只需排除其余尚不支持该
                            # 写入的格式（如 midi，没有音素字段）。
                            _want_devoiced = tr_ja_devoiced_phoneme and fmt in ("sv", "vsqx")
                            # devoiced_target 必须按 fmt 区分：sv/svp 用 SynthesizerV
                            # 罗马字体系（辅音原样保留），vsqx 用 VOCALOID4 官方
                            # 去母音化专属符号（k'/C/p'/S/tS/p\）；否则 SVP 会收到
                            # 它不认识的 VOCALOID4 专属符号导致发音异常。
                            _devoiced_target = "synthesizerv" if fmt in ("sv", "svp") else "vocaloid4"
                            converted = apply_phoneme_mode(
                                seg_tuples, tr_phoneme_mode,
                                with_devoiced_phoneme=_want_devoiced,
                                devoiced_target=_devoiced_target,
                            )
                            if _want_devoiced:
                                segments = [
                                    LabelSegment(
                                        start_time=t0, end_time=t1, label=lbl,
                                        devoiced_phoneme=devoiced,
                                    )
                                    for (t0, t1, lbl, devoiced) in converted
                                ]
                            else:
                                segments = [
                                    LabelSegment(start_time=t0, end_time=t1, label=lbl)
                                    for (t0, t1, lbl) in converted
                                ]
                        except Exception as _pm_err:
                            logger.warning(
                                "[多音轨] 音轨 %r 音素转换失败 (mode=%s): %s，回退到原始音素",
                                tr_title, tr_phoneme_mode, _pm_err,
                            )
                    # 若段落来自 LAB，midi_notes 仍可用于按 MIDI 原始音高设置 tone/音符
                    # （与单文件模式一致：LAB 提供段落，MIDI 提供音高）。
                else:
                    if not midi_notes:
                        logger.warning("[多音轨] 跳过音轨 %r：MIDI 解析失败，无法生成段落", tr_title)
                        continue
                    lyric_words = self._midi_lyrics_to_words(midi_lyrics)
                    segments = self._segments_from_midi_notes(midi_notes, lyric_words=lyric_words)

                # F0 提取（每条音轨独立提取，使用该音轨自己的有效配置
                # tr_config：未开启"单独设置"时与全局 config 完全一致）。
                f0_arr, t_arr = None, None
                try:
                    audio_data = self.process_audio_f0(str(wav_path), tr_config)
                    if audio_data and audio_data.get("success"):
                        f0_arr = audio_data.get("f0")
                        t_arr  = audio_data.get("t")
                    else:
                        logger.warning(
                            "[多音轨] 音轨 %r F0 提取失败(%s)，该音轨将不含音高曲线",
                            tr_title, (audio_data or {}).get("error", "unknown"),
                        )
                except Exception as _f0_err:
                    logger.warning("[多音轨] 音轨 %r F0 提取异常: %s，该音轨将不含音高曲线", tr_title, _f0_err)

                # word_phoneme_map 的原始文本预提取英语单词集合（防拼音误判）
                # ——使用该音轨自己的 tr_language / tr_word_phoneme_map。
                native_english_words: Optional[set] = None
                _lang_norm = (tr_language or "").lower().strip().rstrip("-")
                if tr_word_phoneme_map and _lang_norm not in ("en", "eng", "english") and original_text:
                    try:
                        from phoneme_converter import extract_native_english_words
                        native_english_words = extract_native_english_words(original_text)
                    except Exception as _nee_err:
                        logger.warning("[多音轨] 音轨 %r 预提取英语单词失败: %s，回退词典查询", tr_title, _nee_err)

                # ── 该对话框在合并时间轴上的"时长贡献"：优先用磁盘上的真实
                # wav 时长（音频推断的分段起始时间即基于此累加）；若因文件
                # 损坏等原因读取失败（返回 0），退化为该对话框段落的最大
                # end_time 作为估计时长，保证后续对话框仍能获得合理的起始
                # 位置，不会因为一个对话框异常就把后续内容全部压到时间 0。
                duration_sec = self._read_audio_duration_sec(str(wav_path))
                if duration_sec <= 0:
                    duration_sec = self._lab_time_to_seconds(
                        max((s.end_time for s in segments), default=0)
                    )
                    logger.warning(
                        "[顺序合并] 对话框 %r 无法读取 wav 真实时长，退化使用段落末尾时间估计 (%.3fs)",
                        tr_title, duration_sec,
                    )

                resolved_tracks.append({
                    "title": tr_title,
                    "segments": segments,
                    "f0": f0_arr,
                    "t": t_arr,
                    "native_english_words": native_english_words,
                    "midi_notes": midi_notes,
                    "duration_sec": duration_sec,
                    # ── 该音轨自己的有效设置，供下面的 _sequenced builder
                    # 按每个音轨独立应用词典 / 单词映射 / 语言，而不是整批
                    # 统一一份（bpm 除外，仍使用外层共享 config 的 bpm，
                    # 由 builder 统一负责该字段的时间轴换算）。────────────
                    "word_phoneme_map": tr_word_phoneme_map,
                    "language": tr_language,
                    "dict_source": tr_dict_source,
                    "base_pitch": tr_config.base_pitch,
                    "refine_pitch": tr_config.refine_pitch,
                })

            if not resolved_tracks:
                return {"success": False, "error": "没有任何对话框成功加载（WAV/LAB/MIDI 缺失或无法读取）"}

            # ── 按累计音频时长计算每个对话框在时间轴上的起始位置
            # （offset_sec），但*不*把段落 / F0 / MIDI 音符整体平移拍平进
            # 同一个序列——每个对话框保持自身局部时间不变，只在下面调用
            # _sequenced builder 时通过各自的 offset_sec 定位到时间轴上
            # （SVP: groups[i].blickOffset；USTX: voice_parts[i].position；
            # VSQX: vsPart[i].<t>）。这样每个对话框在编辑器里仍是一段独立
            # 可拖动/编辑的序列，而不是被合并成一条连续音符列表 ─────────
            cursor_sec = 0.0
            for item in resolved_tracks:
                item["offset_sec"] = cursor_sec
                cursor_sec += float(item.get("duration_sec") or 0.0)
            total_duration_sec = cursor_sec

            if fmt == "sv":
                project_text = self._build_svp_project_text_sequenced(
                    project_title=project_title,
                    resolved_tracks=resolved_tracks,
                    config=config,
                    word_phoneme_map=word_phoneme_map,
                    language=language,
                    dict_source=dict_source,
                )
                out_path = self.work_dir / f"{project_title}.svp"
            elif fmt == "ustx":
                project_text = self._build_ustx_project_text_sequenced(
                    project_title=project_title,
                    resolved_tracks=resolved_tracks,
                    config=config,
                )
                out_path = self.work_dir / f"{project_title}.ustx"
            else:  # fmt == "vsqx"
                project_text = self._build_vsqx_project_text_sequenced(
                    project_title=project_title,
                    resolved_tracks=resolved_tracks,
                    config=config,
                    vsqx_singer=vsqx_singer,
                    vsqx_singer_id=vsqx_singer_id,
                    vsqx_singer_bs=vsqx_singer_bs,
                    word_phoneme_map=word_phoneme_map,
                    language=language,
                    dict_source=dict_source,
                )
                out_path = self.work_dir / f"{project_title}.vsqx"

            out_path.write_text(project_text, encoding="utf-8")

            return {
                "success":       True,
                "output_path":   str(out_path),
                "format":        fmt,
                "track_count":   1,
                "segment_count": len(resolved_tracks),
                "total_duration_sec": total_duration_sec,
            }

        except Exception as e:
            logger.error(f"批量顺序拼接工程文件生成失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _midi_lyrics_to_words(self, midi_lyrics) -> List[str]:
        """
        把 MIDI lyrics meta event 转成顺序词表。
        这里复用你现有的 _split_lyrics_to_words 逻辑。
        """
        words: List[str] = []
        if not midi_lyrics:
            return words

        for ev in midi_lyrics:
            txt = (ev.text or "").strip()
            if not txt:
                continue
            words.extend(_split_lyrics_to_words(txt))
        return words


    def _apply_midi_lyrics_to_segments(self, segments, lyric_words: List[str]):
        """
        用 MIDI 歌词覆盖段落 label。
        静音段只在歌词当前位置也是占位符 '-' 时才消费一个 token，
        这样可以避免把后面的词前移。
        """
        if not lyric_words:
            return segments

        silence_tokens = {"sil", "pau", "sp", "spn", "rest"}
        out = []
        idx = 0

        for seg in segments:
            label = (seg.label or "").strip()
            lower = label.lower()

            if lower in silence_tokens:
                if idx < len(lyric_words) and lyric_words[idx] == "-":
                    out.append(
                        LabelSegment(
                            start_time=seg.start_time,
                            end_time=seg.end_time,
                            label="-",
                        )
                    )
                    idx += 1
                else:
                    out.append(seg)
                continue

            if idx < len(lyric_words):
                new_label = lyric_words[idx]
                idx += 1
            else:
                new_label = label

            out.append(
                LabelSegment(
                    start_time=seg.start_time,
                    end_time=seg.end_time,
                    label=new_label,
                )
            )

        return out

    # ----------------------------
    # VSQX 生成
    # ----------------------------
    _VSQX_PART_OFFSET = 1920   # vsPart 默认起始 tick（preMeasure=1, 4/4）

    def _build_vsqx_part_xml(
        self,
        segments: List[LabelSegment],
        f0: Optional[np.ndarray],
        t: Optional[np.ndarray],
        config: AudioProcessingConfig,
        midi_notes: Optional[List] = None,
        vsqx_singer_bs: int = 4,
        word_phoneme_map: bool = False,
        language: str = "",
        native_english_words: Optional[set] = None,
        dict_source: str = "default",
        t_start: Optional[int] = None,
        part_name: str = "NewPart",
    ) -> str:
        """
        构建单个 VSQX <vsPart> XML 块（音符 / 音高曲线），供：
        · _build_vsqx_track_block()（单文件 / "背靠背"多音轨旧逻辑，每条
          vsTrack 恰好一个 vsPart，固定挂在 t_start=_VSQX_PART_OFFSET）
        · _build_vsqx_project_text_sequenced()（"独立序列 + 时间轴定位"
          新逻辑，一条 vsTrack 下挂多个 vsPart，每个 vsPart 各自的
          t_start = _VSQX_PART_OFFSET + 该对话框在时间轴上的累计偏移量）
        共用，避免逻辑重复导致行为不一致。

        segments / f0 / t / midi_notes 全部使用"局部时间"（相对该 vsPart
        自身起点为 0，不做整体平移）——vsPart 内 <note><t> 是相对 vsPart
        自身 <t> 的偏移量，音符的绝对时间轴位置 = vsPart.<t> + note.<t>，
        因此只需通过 t_start 整体挪动 vsPart，不需要改动 segments 本身。
        """
        RESOLUTION  = 480        # ticks per quarter note
        PBS         = 13         # pitch bend sensitivity (semitones)
        if t_start is None:
            t_start = self._VSQX_PART_OFFSET
        VELOCITY    = 64
        bpm = float(config.bpm)

        def sec_to_ticks(sec: float) -> int:
            """秒 → vsPart 内部 tick（相对偏移）"""
            return int(round(float(sec) * (bpm / 60.0) * RESOLUTION))

        base_tone = int(config.base_pitch)

        # ── ① 构建音符列表 ──────────────────────────────────────────────────
        # voiced_refs 用于音高曲线中查找该 tick 对应的音符 tone
        note_entries: List[Tuple[int, int, int, str, str]] = []  # (t_on, dur, tone, lyric, p_tag)
        voiced_refs:  List[Tuple[int, int, int]]           = []  # (t_on, t_off, tone)

        # 预加载 word_phoneme_map / 词典查询所需函数（惰性导入）。
        # 只要开关已开启，或用户选择了自定义词典（dict_source != "default"），
        # 就需要 arpabet_to_vocaloid4（词典记号为 ARPABET 时转换用）；
        # word_to_arpabet 仅在开关开启时才需要（词典未命中的兜底转换）。
        _word_to_arpabet   = None
        _arpabet_to_vocaloid4 = None
        _dict_selected = bool(dict_source) and dict_source != "default"
        if word_phoneme_map or _dict_selected:
            try:
                from phoneme_converter import arpabet_to_vocaloid4 as _atv4
                _arpabet_to_vocaloid4 = _atv4
                if word_phoneme_map:
                    from phoneme_converter import word_to_arpabet as _wta
                    _word_to_arpabet = _wta
            except Exception as _imp_err:
                logger.warning("[VSQX word_phoneme_map] 导入失败: %s，功能已禁用", _imp_err)

        for seg in segments:
            if seg.end_time <= seg.start_time:
                continue
            if self._is_true_silence(seg.label):
                continue

            start_sec = self._lab_time_to_seconds(seg.start_time)
            end_sec   = self._lab_time_to_seconds(seg.end_time)
            t_on  = sec_to_ticks(start_sec)
            t_off = max(t_on + 1, sec_to_ticks(end_sec))
            dur   = t_off - t_on

            # 音高：MIDI / F0细化 / base_pitch 三级优先
            tone = base_tone
            _seg_has_midi = midi_notes is not None and self._midi_notes_overlap_segment(
                start_sec, end_sec, midi_notes
            )
            if _seg_has_midi:
                # 仅当该段时间范围内确有 MIDI 音符重叠时才采用；顺序合并
                # 多个对话框时，没有 MIDI 的对话框段落不会被其它对话框的
                # midi_notes 误判命中，见 _midi_notes_overlap_segment
                from midi_processor import map_segment_to_midi_pitch
                tone = map_segment_to_midi_pitch(
                    start_sec, end_sec, midi_notes, base_pitch=base_tone)
            elif config.refine_pitch and f0 is not None and t is not None and len(f0) > 0:
                mask  = (t >= start_sec) & (t < end_sec)
                seg_f0 = f0[mask]
                voiced = seg_f0[seg_f0 > 0]
                if len(voiced) > 0:
                    midi_vals = 69.0 + 12.0 * np.log2(voiced / 440.0)
                    tone      = int(round(float(np.median(midi_vals))))
                    tone      = max(12, min(127, tone))

            label = seg.label

            # ── 音素写入 <p> 字段：词典命中 与 word_phoneme_map 开关解耦 ─────
            # <p></p>                    → VOCALOID 自带 G2P（默认）
            # <p lock="1"><![CDATA[…]]></p>  → 手工音素 + 锁定（phoneme lock）
            #
            # 【解耦说明】与 SVP 分支同理：只要选择了自定义词典
            # （dict_source != "default"）且命中该词，就应写入 <p lock="1">，
            # 不要求用户额外打开 word_phoneme_map 开关；该开关只控制"词典
            # 未命中时，是否兜底走软件默认 G2P（word_to_arpabet →
            # arpabet_to_vocaloid4）转换全部英语单词"。
            #
            # 【重要】词典查询本身不受 _label_is_english_word 语言守卫限制，
            # 理由与 SVP 分支相同：词典条目不局限于真实英语单词，也可以是
            # ARPABET/VOCALOID 音素符号本身（跨语种英语在 MFA 里回退到音素级
            # 对齐时，label 可能直接是 "ah"/"ch" 这样的音素符号）。语言守卫
            # （native_english_words / is_in_english_dict，用于防止拼音
            # rang/wang/dong 等误判为英语词）只应用于 G2P 兜底
            # （word_to_arpabet），不应用于词典查询，否则词典里明明存在的
            # 音素会因 label 未通过"真实英语单词"判定而被跳过、不被写入。
            p_tag = '<p></p>'

            # ── 日语去母音化音素写入（ja_devoiced_phoneme）：与英语
            # word_phoneme_map / 自定义词典路径完全解耦，各走各的，互不影响。
            # seg.devoiced_phoneme 由 process_full_pipeline() 在 phoneme_mode
            # 为 merge/hiragana/katakana 且 ja_devoiced_phoneme=True 时，通过
            # apply_phoneme_mode(..., with_devoiced_phoneme=True) 预先算出，
            # 仅当该合并音节的元音在原始 LAB 中被标记为去母音化（大写 I/U）
            # 时才有值，为该音节对应的 VOCALOID4 官方去母音化音素符号
            # （据 VOCALOID4 Editor manual 附录音素表，通过
            # phoneme_converter.ja_devoiced_onset_to_vocaloid4() 解析）——
            # 多数行去母音化后辅音符号不变（す→su 去母音化后仍是 's'，
            # つ→tsu 仍是 'ts'），但き/ひ/ぴ有专属去母音化记号（き→ki 去
            # 母音化后是 "k'" 而非 'k'；ひ→hi 是 "C"；ぴ→pi 是 "p'"），
            # ふ固定使用 "p\\"（与是否去母音化无关）。有浊音的音节
            # （如 ご/go）以及单独元音 / ん・ン・N / っ・ッ / 静音段的
            # devoiced_phoneme 恒为 None，此时保持 <p></p>，交给 VOCALOID
            # 自带 G2P 处理。
            # VSQX 专属：写入后额外加 lock="1" 锁定，防止用户误触发自动重新
            # 发音；SVP 没有锁定概念，只写入音素本身（见 _build_svp_track_payload）。
            if getattr(seg, "devoiced_phoneme", None):
                p_tag = f'<p lock="1"><![CDATA[{seg.devoiced_phoneme}]]></p>'
            elif (
                (word_phoneme_map or _dict_selected)
                and self._is_ascii_word_label(label)
            ):
                try:
                    v4_phones: Optional[str] = None

                    # ── 优先查询用户自定义词典 ──────────────────────────────
                    # notation="vocaloid"：词条已是 VOCALOID4 记号，直接使用，
                    #   跳过 arpabet_to_vocaloid4 转换（否则会被错误地二次转换）。
                    # notation="synthesizerv"：词条是 ARPABET 记号，仍需转换成
                    #   VOCALOID4 记号后才能写入 <p lock="1">。
                    # 注意：判断记号要用词典的 notation 元数据（get_notation），
                    # 不能直接拿 dict_source（词典名，用户自定义任意字符串）
                    # 跟字面量 "vocaloid"/"synthesizerv" 比较——词典名和记号是
                    # 两回事，否则用户自建的词典（例如命名为"你好"）永远不会
                    # 被命中。
                    if _dict_selected:
                        from dictionary_manager import get_notation as _dict_notation
                        from dictionary_manager import lookup_word as _dict_lookup
                        notation = _dict_notation(dict_source)
                        if notation in ("vocaloid", "synthesizerv"):
                            user_phones_str = _dict_lookup(label, dict_source)
                            if user_phones_str:
                                if notation == "vocaloid":
                                    v4_phones = user_phones_str
                                elif _arpabet_to_vocaloid4 is not None:
                                    v4_phones = _arpabet_to_vocaloid4(user_phones_str.split())
                                if v4_phones:
                                    logger.debug(
                                        "[VSQX word_phoneme_map] 用户词典命中(%s) %r → V4 %s",
                                        notation, label, v4_phones,
                                    )

                    # G2P 兜底（word_to_arpabet → arpabet_to_vocaloid4）：仅在
                    # 词典未命中、且用户开启了 word_phoneme_map 开关时才走，
                    # 并且必须通过语言守卫（_label_is_english_word），防止
                    # 拼音误判——这是唯一需要该守卫的地方。
                    if (
                        v4_phones is None
                        and word_phoneme_map
                        and _word_to_arpabet is not None
                        and self._label_is_english_word(label, language, native_english_words)
                    ):
                        arpabet_phones = _word_to_arpabet(label)
                        if arpabet_phones and _arpabet_to_vocaloid4 is not None:
                            v4_phones = _arpabet_to_vocaloid4(arpabet_phones)
                            logger.debug(
                                "[VSQX word_phoneme_map] %r → ARPABET %s → V4 %s",
                                label, " ".join(arpabet_phones), v4_phones,
                            )

                    if v4_phones:
                        p_tag = f'<p lock="1"><![CDATA[{v4_phones}]]></p>'
                except Exception as _wp_err:
                    logger.warning(
                        "[VSQX word_phoneme_map] 转换失败 %r: %s", label, _wp_err
                    )

            note_entries.append((t_on, dur, tone, label, p_tag))
            voiced_refs.append((t_on, t_off, tone))

        # ── ② 音高曲线（P 控制器）───────────────────────────────────────────
        pit_lines: List[str] = []
        if config.export_pitch_line and f0 is not None and t is not None and len(f0) > 0:
            # voiced_refs 已按时间顺序
            vr_arr = np.array(voiced_refs, dtype=np.int64) if voiced_refs else None

            # 先收集每个有声帧的绝对 MIDI 音高。必须先平滑绝对音高、再减去
            # 当前音符 nominal tone；如果直接平滑 PIT offset，音符边界处因
            # nominal tone 改变而产生的合法阶跃会被误当成噪声，反而制造回弹。
            raw_ticks:   List[int]   = []
            raw_pitch:   List[float] = []
            raw_idx:     List[int]   = []

            for i, (ti, f0i) in enumerate(zip(t, f0)):
                if not np.isfinite(f0i) or f0i <= 0:
                    continue
                tick    = sec_to_ticks(float(ti))
                midi_f0 = 69.0 + 12.0 * np.log2(float(f0i) / 440.0)

                # 二分查找当前 tick 所属音符的 tone。
                #
                # 【修复】此前找不到所属音符时会 fallback 到 base_tone（工程
                # 默认基准音，如 C4），但这是错的——这些 tick 通常落在两个
                # 音符之间的静音间隙里（换气/停顿，比如逐字唱的短音符之间
                # 常见几百 tick 的空隙）。process_audio_f0 里对无声段做过
                # 线性插值桥接，这段间隙内 f0 数组并不是真正的 0，而是插值
                # 出来的过渡值，本身就不代表任何真实音高；用 base_tone 当
                # nominal 算出的 offset 因此毫无意义，还经常是几千 cent 的
                # 离谱跳变（真实音高 − 固定基准音，而不是 − 附近音符音高）。
                # 更严重的是，这类"assign 到 base_tone"的帧会被当成一次
                # nominal 变化，在音符间隙的开头和结尾各触发一次"伪音符边界"，
                # 把平滑/降采样切成大量细碎小段——这正是"改用按音符边界分段"
                # 之后锯齿反而更明显、且是规律阶梯状的原因。
                # 现在改为：找不到所属音符（不在任何 [t_on, t_off] 区间内）
                # 就直接跳过这一帧，不写入 PIT 曲线——没有音符可弯，也就不
                # 需要在这里给一个虚假的点。
                nominal = None
                if vr_arr is not None and len(vr_arr):
                    idx = int(np.searchsorted(vr_arr[:, 0], tick, side="right")) - 1
                    if 0 <= idx < len(vr_arr) and tick <= vr_arr[idx, 1]:
                        nominal = int(vr_arr[idx, 2])
                elif t is not None and len(t) > 0:
                    # 没有音符信息（voiced_refs 为空）时退回旧行为，仍用
                    # base_tone，保证没有音符结构时功能不受影响。
                    nominal = base_tone

                if nominal is None:
                    continue

                raw_ticks.append(tick)
                raw_pitch.append(midi_f0)
                raw_idx.append(i)

            if raw_pitch:
                pitches = np.asarray(raw_pitch, dtype=np.float64)
                idx_arr = np.asarray(raw_idx, dtype=np.int64)

                # 只在 F0 采样下标真正不连续时断开。音符边界不再是平滑
                # 断点，因为这里处理的是绝对音高；连续发声跨音符时应保持
                # 连续，最后换算 PIT 时再根据每个 tick 的 nominal tone
                # 产生必要的控制器跳变。
                if len(idx_arr) >= 2:
                    breaks_all = np.where(np.diff(idx_arr) > 1)[0] + 1
                else:
                    breaks_all = np.array([], dtype=np.int64)

                # ── PIT 曲线 Hann 平滑 ──────────────────────────────────────
                # 使用 Hann 窗而不是箱式移动平均。输入已经是
                # MIDI/半音域，因此与人的音高感知及 PIT 的线性单位一致。
                window = int(getattr(config, "vsqx_pitch_smooth_window", 0) or 0)
                if window > 1 and len(pitches) >= 3:
                    smoothed = pitches.copy()
                    runs = np.split(np.arange(len(idx_arr)), breaks_all)
                    for run in runs:
                        if len(run) < 3:
                            continue
                        smoothed[run] = self._hann_smooth_1d(pitches[run], window)
                    pitches = smoothed

                # ── 以约 2ms 间隔稠密写入 PIT ──────────────────────────────
                # VOCALOID4 对相邻 PIT <cc> 事件采用保持值显示；旧实现错误地
                # 假定编辑器会在线性插值后按 40ms 分桶，结果正是截图中的宽
                # 台阶/锯齿。参考以 2ms 生成音高点，这里同样在平滑后
                # 对绝对音高做线性重采样，再写成稠密控制点。
                point_step_ticks = max(1, sec_to_ticks(0.002))
                ticks_arr = np.asarray(raw_ticks, dtype=np.int64)
                runs = np.split(np.arange(len(idx_arr)), breaks_all)
                dense_ticks: List[int] = []
                dense_pitches: List[float] = []

                for run in runs:
                    if len(run) == 0:
                        continue

                    seg_t = ticks_arr[run]
                    seg_p = pitches[run]

                    # 秒→tick 后可能有重复 tick；先按 tick 求平均，保证 np.interp
                    # 的横坐标严格递增。
                    unique_t, inverse = np.unique(seg_t, return_inverse=True)
                    if len(unique_t) != len(seg_t):
                        sums = np.zeros(len(unique_t), dtype=np.float64)
                        counts = np.zeros(len(unique_t), dtype=np.int64)
                        np.add.at(sums, inverse, seg_p)
                        np.add.at(counts, inverse, 1)
                        unique_p = sums / np.maximum(counts, 1)
                    else:
                        unique_p = seg_p

                    if len(unique_t) == 1:
                        grid = unique_t.copy()
                    else:
                        grid = np.arange(
                            int(unique_t[0]),
                            int(unique_t[-1]) + 1,
                            point_step_ticks,
                            dtype=np.int64,
                        )
                        if grid[-1] != unique_t[-1]:
                            grid = np.append(grid, unique_t[-1])

                        # 必须在每个音符起点精确写一个新 PIT 值，否则上一音符
                        # 的 PIT 会在新音符开始后继续保持若干毫秒，造成起音跳变。
                        if vr_arr is not None and len(vr_arr):
                            note_starts = vr_arr[:, 0]
                            note_starts = note_starts[
                                (note_starts >= unique_t[0]) &
                                (note_starts <= unique_t[-1])
                            ]
                            if len(note_starts):
                                grid = np.unique(np.concatenate((grid, note_starts)))

                    grid_p = np.interp(grid, unique_t, unique_p)
                    dense_ticks.extend(int(x) for x in grid)
                    dense_pitches.extend(float(x) for x in grid_p)

                prev_val: Optional[int] = None
                for tick, midi_pitch in zip(dense_ticks, dense_pitches):
                    nominal = None
                    if vr_arr is not None and len(vr_arr):
                        note_idx = int(np.searchsorted(vr_arr[:, 0], tick, side="right")) - 1
                        if (
                            0 <= note_idx < len(vr_arr)
                            and tick < int(vr_arr[note_idx, 1])
                        ):
                            nominal = int(vr_arr[note_idx, 2])
                    else:
                        nominal = base_tone

                    # 重采样网格可能穿过两个音符之间的空隙；空隙内不写 PIT。
                    if nominal is None:
                        continue

                    offset = midi_pitch - nominal
                    # 偏移量（半音）→ P 控制器值（范围 ±8190）
                    p_val = int(round(float(offset) / PBS * 8190))
                    p_val = max(-8190, min(8190, p_val))

                    # 压缩相邻重复值
                    if p_val != prev_val:
                        pit_lines.append(
                            f'\t\t\t<cc><t>{tick}</t><v id="P">{p_val}</v></cc>'
                        )
                        prev_val = p_val

        # ── ③ 计算 vsPart 总长度 ─────────────────────────────────────────────
        if note_entries:
            play_time = max(t_on + dur for t_on, dur, *_ in note_entries) + RESOLUTION
        else:
            play_time = RESOLUTION * 4   # 至少一小节

        # ── ④ 生成 XML ───────────────────────────────────────────────────────
        # 首行：PBS 设置（必须在其他 cc 之前）
        pit_block = f'\t\t\t<cc><t>0</t><v id="S">{PBS}</v></cc>\n'
        if pit_lines:
            pit_block += "\n".join(pit_lines) + "\n"

        # 音符块
        note_block = ""
        for t_on, dur, tone, lyric, p_tag in note_entries:
            note_block += (
                f'\t\t\t<note>\n'
                f'\t\t\t\t<t>{t_on}</t>\n'
                f'\t\t\t\t<dur>{dur}</dur>\n'
                f'\t\t\t\t<n>{tone}</n>\n'
                f'\t\t\t\t<v>{VELOCITY}</v>\n'
                f'\t\t\t\t<y><![CDATA[{lyric}]]></y>\n'
                f'\t\t\t\t{p_tag}\n'
                f'\t\t\t\t<nStyle>\n'
                f'\t\t\t\t\t<v id="accent">50</v>\n'
                f'\t\t\t\t</nStyle>\n'
                f'\t\t\t</note>\n'
            )

        part_xml = (
            '\t\t<vsPart>\n'
            f'\t\t\t<t>{t_start}</t>\n'
            f'\t\t\t<playTime>{play_time}</playTime>\n'
            f'\t\t\t<name><![CDATA[{part_name}]]></name>\n'
            '\t\t\t<comment><![CDATA[New Musical Part]]></comment>\n'
            '\t\t\t<sPlug>\n'
            '\t\t\t\t<id><![CDATA[ACA9C502-A04B-42b5-B2EB-5CEA36D16FCE]]></id>\n'
            '\t\t\t\t<name><![CDATA[VOCALOID2 Compatible Style]]></name>\n'
            '\t\t\t\t<version><![CDATA[3.0.0.1]]></version>\n'
            '\t\t\t</sPlug>\n'
            '\t\t\t<pStyle>\n'
            '\t\t\t\t<v id="accent">50</v>\n'
            '\t\t\t\t<v id="bendDep">8</v>\n'
            '\t\t\t\t<v id="bendLen">0</v>\n'
            '\t\t\t\t<v id="decay">50</v>\n'
            '\t\t\t\t<v id="fallPort">0</v>\n'
            '\t\t\t\t<v id="opening">127</v>\n'
            '\t\t\t\t<v id="risePort">0</v>\n'
            '\t\t\t</pStyle>\n'
            '\t\t\t<singer>\n'
            '\t\t\t\t<t>0</t>\n'
            f'\t\t\t\t<bs>{vsqx_singer_bs}</bs>\n'
            '\t\t\t\t<pc>0</pc>\n'
            '\t\t\t</singer>\n'
            + pit_block
            + note_block
            + '\t\t</vsPart>\n'
        )
        return part_xml

    def _build_vsqx_track_block(
        self,
        tNo: int,
        name: str,
        segments: List[LabelSegment],
        f0: Optional[np.ndarray],
        t: Optional[np.ndarray],
        config: AudioProcessingConfig,
        midi_notes: Optional[List] = None,
        vsqx_singer_bs: int = 4,
        word_phoneme_map: bool = False,
        language: str = "",
        native_english_words: Optional[set] = None,
        dict_source: str = "default",
    ) -> str:
        """
        构建单条 VSQX <vsTrack> XML 块（tNo + 恰好一个 vsPart）。

        供单音轨 (_build_vsqx_project_text) 与"背靠背"旧版多音轨
        (build_vsqx_project_text_multitrack，每个对话框一条独立 vsTrack) 共用。
        实际的 vsPart / 音符 / 音高曲线构建已提取到 _build_vsqx_part_xml()，
        与新版 _build_vsqx_project_text_sequenced()（同一条 vsTrack 下挂多个
        vsPart，各自定位到时间轴）共用，避免逻辑重复导致行为不一致。
        """
        part_xml = self._build_vsqx_part_xml(
            segments=segments,
            f0=f0,
            t=t,
            config=config,
            midi_notes=midi_notes,
            vsqx_singer_bs=vsqx_singer_bs,
            word_phoneme_map=word_phoneme_map,
            language=language,
            native_english_words=native_english_words,
            dict_source=dict_source,
            t_start=self._VSQX_PART_OFFSET,
            part_name="NewPart",
        )
        return (
            '\t<vsTrack>\n'
            f'\t\t<tNo>{tNo}</tNo>\n'
            f'\t\t<name><![CDATA[{name}]]></name>\n'
            '\t\t<comment><![CDATA[Track]]></comment>\n'
            + part_xml +
            '\t</vsTrack>\n'
        )

    @staticmethod
    def _vsqx_vs_unit_block(tNo: int) -> str:
        """mixer.vsUnit 块（每条音轨一个，tNo 与对应 vsTrack 一致）。"""
        return (
            '\t\t<vsUnit>\n'
            f'\t\t\t<tNo>{tNo}</tNo>\n'
            '\t\t\t<iGin>0</iGin>\n'
            '\t\t\t<sLvl>-898</sLvl>\n'
            '\t\t\t<sEnable>0</sEnable>\n'
            '\t\t\t<m>0</m>\n'
            '\t\t\t<s>0</s>\n'
            '\t\t\t<pan>64</pan>\n'
            '\t\t\t<vol>0</vol>\n'
            '\t\t</vsUnit>\n'
        )

    def _build_vsqx_project_text(
        self,
        title: str,
        segments: List[LabelSegment],
        f0: Optional[np.ndarray],
        t: Optional[np.ndarray],
        sr: Optional[int],
        wav_path: str,
        config: AudioProcessingConfig,
        audio_duration_sec: Optional[float] = None,
        midi_notes: Optional[List] = None,
        vsqx_singer: str = "MIKU_V4_Chinese",
        vsqx_singer_id: str = "BNGE7CP7EMTRSNC3",
        vsqx_singer_bs: int = 4,           # VOCALOID4 内部声库 Bank Select 编号（系统相关）
        word_phoneme_map: bool = False,    # 英语单词 → VOCALOID4 音素写入 <p lock="1">
        language: str = "",                # 语种（用于 word_phoneme_map 防误判）
        native_english_words: Optional[set] = None,  # 从原始文本预提取的英语单词集合（防拼音误判）
        dict_source: str = "default",      # 单词→音素词典来源："default"/"synthesizerv"/"vocaloid"
    ) -> str:
        """
        生成 VOCALOID4 VSQX 工程文件（XML 格式，单音轨）。

        格式要点
        ────────
        • resolution = 480 ticks/拍
        • tempo 写法：<tempo><t>0</t><v>{BPM×100}</v></tempo>
          例：120 BPM → v=12000；实际文件验证一致。
        • vsPart 从 tick=1920 开始（preMeasure=1，4/4，480×4=1920）
        • note 和 cc（音高曲线）的 <t> 均为相对于 vsPart 起点的偏移
        • 音高曲线（P 控制器）范围 ±8190，对应 ±PBS 个半音
          PBS 默认值 = 12（来自实际 vsqx 文件）
        • 歌词 <y>：使用 segment label；<p> 音素字段默认留空，
          由 VOCALOID 自带 G2P 处理（用户可在 VOCALOID Editor 中手动编辑）。
          若 segment.devoiced_phoneme 有值（日语音素锁定开关 ja_devoiced_phoneme
          开启、且该音节含辅音起始），则写入 <p lock="1">…</p> 并锁定为
          该辅音起始（如 's'/'sh'/'ky'），跳过 VOCALOID 自带 G2P。

        实际的音符 / 音高曲线 / <vsTrack> XML 构建逻辑已提取到
        _build_vsqx_track_block()，与对话文本框批量处理功能的多音轨生成器
        共用，本方法只负责组装单音轨的 vVoiceTable/mixer/masterTrack 等
        工程级别结构，并拼接唯一一条 <vsTrack>（tNo=0）。
        """
        bpm = float(config.bpm)
        tempo_v = int(round(bpm * 100))   # VSQX tempo value = BPM × 100

        track_xml = self._build_vsqx_track_block(
            tNo=0,
            name=title,
            segments=segments,
            f0=f0,
            t=t,
            config=config,
            midi_notes=midi_notes,
            vsqx_singer_bs=vsqx_singer_bs,
            word_phoneme_map=word_phoneme_map,
            language=language,
            native_english_words=native_english_words,
            dict_source=dict_source,
        )

        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
            '<vsq4 xmlns="http://www.yamaha.co.jp/vocaloid/schema/vsq4/"\n'
            '      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            '      xsi:schemaLocation="http://www.yamaha.co.jp/vocaloid/schema/vsq4/'
            ' vsq4.xsd">\n'
            '\t<vender><![CDATA[Yamaha corporation]]></vender>\n'
            '\t<version><![CDATA[4.0.0.3]]></version>\n'
            '\t<vVoiceTable>\n'
            '\t\t<vVoice>\n'
            f'\t\t\t<bs>{vsqx_singer_bs}</bs>\n'
            '\t\t\t<pc>0</pc>\n'
            f'\t\t\t<id><![CDATA[{vsqx_singer_id}]]></id>\n'
            f'\t\t\t<name><![CDATA[{vsqx_singer}]]></name>\n'
            '\t\t\t<vPrm>\n'
            '\t\t\t\t<bre>0</bre>\n'
            '\t\t\t\t<bri>0</bri>\n'
            '\t\t\t\t<cle>0</cle>\n'
            '\t\t\t\t<gen>0</gen>\n'
            '\t\t\t\t<ope>0</ope>\n'
            '\t\t\t</vPrm>\n'
            '\t\t</vVoice>\n'
            '\t</vVoiceTable>\n'
            '\t<mixer>\n'
            '\t\t<masterUnit>\n'
            '\t\t\t<oDev>0</oDev>\n'
            '\t\t\t<rLvl>0</rLvl>\n'
            '\t\t\t<vol>0</vol>\n'
            '\t\t</masterUnit>\n'
            + self._vsqx_vs_unit_block(0)
            + '\t\t<monoUnit>\n'
            '\t\t\t<iGin>0</iGin>\n'
            '\t\t\t<sLvl>-898</sLvl>\n'
            '\t\t\t<sEnable>0</sEnable>\n'
            '\t\t\t<m>0</m>\n'
            '\t\t\t<s>0</s>\n'
            '\t\t\t<pan>64</pan>\n'
            '\t\t\t<vol>0</vol>\n'
            '\t\t</monoUnit>\n'
            '\t\t<stUnit>\n'
            '\t\t\t<iGin>0</iGin>\n'
            '\t\t\t<m>0</m>\n'
            '\t\t\t<s>0</s>\n'
            '\t\t\t<vol>-129</vol>\n'
            '\t\t</stUnit>\n'
            '\t</mixer>\n'
            '\t<masterTrack>\n'
            f'\t\t<seqName><![CDATA[{title}]]></seqName>\n'
            '\t\t<comment><![CDATA[New VSQ File]]></comment>\n'
            '\t\t<resolution>480</resolution>\n'
            '\t\t<preMeasure>1</preMeasure>\n'
            '\t\t<timeSig><m>0</m><nu>4</nu><de>4</de></timeSig>\n'
            f'\t\t<tempo><t>0</t><v>{tempo_v}</v></tempo>\n'
            '\t</masterTrack>\n'
            + track_xml
            + '\t<monoTrack>\n'
            '\t</monoTrack>\n'
            '\t<stTrack>\n'
            '\t</stTrack>\n'
            '\t<aux>\n'
            '\t\t<id><![CDATA[AUX_VST_HOST_CHUNK_INFO]]></id>\n'
            '\t\t<content>'
            '<![CDATA[VlNDSwAAAAADAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=]]>'
            '</content>\n'
            '\t</aux>\n'
            '</vsq4>\n'
        )
        return xml

    def _build_vsqx_project_text_sequenced(
        self,
        project_title: str,
        resolved_tracks: List[Dict],
        config: AudioProcessingConfig,
        vsqx_singer: str = "MIKU_V4_Chinese",
        vsqx_singer_id: str = "BNGE7CP7EMTRSNC3",
        vsqx_singer_bs: int = 4,
        word_phoneme_map: bool = False,
        language: str = "",
        dict_source: str = "default",
    ) -> str:
        """
        对话文本框批量处理功能的 VSQX 生成入口（"独立序列 + 时间轴定位"模式）：
        全部对话框写入*同一条* <vsTrack>（tNo=0），但每个对话框各自占一个
        独立的 <vsPart>（而不是把音符拍平合并进同一个 vsPart），每个 vsPart
        的音符仍使用该对话框自身的局部时间（相对该 vsPart 起点为 0），
        只通过 vsPart.<t> 整体定位到时间轴上的位置：
            vsPart[i].<t> = _VSQX_PART_OFFSET + 该对话框在合并时间轴上的
                            起始时间（resolved_tracks[i]["offset_sec"]，
                            由调用方按累计 wav 时长算好，转换为 tick）
        这样每个对话框在 VOCALOID Editor 里仍是可独立拖动/编辑的 Part，
        而不是被合并成一条连续音符列表。

        Parameters
        ----------
        resolved_tracks : 每个元素为 build_multitrack_project 里已解析好的
            单个对话框数据（segments / f0 / t / midi_notes 均为该对话框
            自身的局部时间，未做整体平移），额外需要 "offset_sec" 字段
            （该对话框在合并时间轴上的起始秒数）。

        Returns
        -------
        str : VSQX XML 文本，仅含一条 <vsTrack>，其下有 len(resolved_tracks)
              个 <vsPart>。
        """
        bpm = float(config.bpm)
        tempo_v = int(round(bpm * 100))
        ticks_per_sec = (bpm / 60.0) * 480.0

        from dataclasses import replace as _dc_replace

        part_blocks = ""
        for tr in resolved_tracks:
            t_start = self._VSQX_PART_OFFSET + int(round(float(tr.get("offset_sec") or 0.0) * ticks_per_sec))

            # ── 该音轨自己的有效设置：与 _build_svp_project_text_sequenced
            # 同样的覆盖规则——bpm 恒定使用全局 config.bpm（不接受按音轨
            # 覆盖，见该函数上方 docstring），只替换 base_pitch / refine_pitch，
            # word_phoneme_map / language / dict_source 按音轨独立生效。──
            tr_config = _dc_replace(
                config,
                base_pitch=tr.get("base_pitch", config.base_pitch),
                refine_pitch=tr.get("refine_pitch", config.refine_pitch),
            )
            tr_word_phoneme_map = tr.get("word_phoneme_map", word_phoneme_map)
            tr_language = tr.get("language", language)
            tr_dict_source = tr.get("dict_source", dict_source)

            part_blocks += self._build_vsqx_part_xml(
                segments=tr.get("segments") or [],
                f0=tr.get("f0"),
                t=tr.get("t"),
                config=tr_config,
                midi_notes=tr.get("midi_notes"),
                vsqx_singer_bs=vsqx_singer_bs,
                word_phoneme_map=tr_word_phoneme_map,
                language=tr_language,
                native_english_words=tr.get("native_english_words"),
                dict_source=tr_dict_source,
                t_start=t_start,
                part_name=(tr.get("title") or "NewPart"),
            )

        track_xml = (
            '\t<vsTrack>\n'
            '\t\t<tNo>0</tNo>\n'
            f'\t\t<name><![CDATA[{project_title}]]></name>\n'
            '\t\t<comment><![CDATA[Track]]></comment>\n'
            + part_blocks +
            '\t</vsTrack>\n'
        )

        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
            '<vsq4 xmlns="http://www.yamaha.co.jp/vocaloid/schema/vsq4/"\n'
            '      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            '      xsi:schemaLocation="http://www.yamaha.co.jp/vocaloid/schema/vsq4/'
            ' vsq4.xsd">\n'
            '\t<vender><![CDATA[Yamaha corporation]]></vender>\n'
            '\t<version><![CDATA[4.0.0.3]]></version>\n'
            '\t<vVoiceTable>\n'
            '\t\t<vVoice>\n'
            f'\t\t\t<bs>{vsqx_singer_bs}</bs>\n'
            '\t\t\t<pc>0</pc>\n'
            f'\t\t\t<id><![CDATA[{vsqx_singer_id}]]></id>\n'
            f'\t\t\t<name><![CDATA[{vsqx_singer}]]></name>\n'
            '\t\t\t<vPrm>\n'
            '\t\t\t\t<bre>0</bre>\n'
            '\t\t\t\t<bri>0</bri>\n'
            '\t\t\t\t<cle>0</cle>\n'
            '\t\t\t\t<gen>0</gen>\n'
            '\t\t\t\t<ope>0</ope>\n'
            '\t\t\t</vPrm>\n'
            '\t\t</vVoice>\n'
            '\t</vVoiceTable>\n'
            '\t<mixer>\n'
            '\t\t<masterUnit>\n'
            '\t\t\t<oDev>0</oDev>\n'
            '\t\t\t<rLvl>0</rLvl>\n'
            '\t\t\t<vol>0</vol>\n'
            '\t\t</masterUnit>\n'
            + self._vsqx_vs_unit_block(0)
            + '\t\t<monoUnit>\n'
            '\t\t\t<iGin>0</iGin>\n'
            '\t\t\t<sLvl>-898</sLvl>\n'
            '\t\t\t<sEnable>0</sEnable>\n'
            '\t\t\t<m>0</m>\n'
            '\t\t\t<s>0</s>\n'
            '\t\t\t<pan>64</pan>\n'
            '\t\t\t<vol>0</vol>\n'
            '\t\t</monoUnit>\n'
            '\t\t<stUnit>\n'
            '\t\t\t<iGin>0</iGin>\n'
            '\t\t\t<m>0</m>\n'
            '\t\t\t<s>0</s>\n'
            '\t\t\t<vol>-129</vol>\n'
            '\t\t</stUnit>\n'
            '\t</mixer>\n'
            '\t<masterTrack>\n'
            f'\t\t<seqName><![CDATA[{project_title}]]></seqName>\n'
            '\t\t<comment><![CDATA[New VSQ File]]></comment>\n'
            '\t\t<resolution>480</resolution>\n'
            '\t\t<preMeasure>1</preMeasure>\n'
            '\t\t<timeSig><m>0</m><nu>4</nu><de>4</de></timeSig>\n'
            f'\t\t<tempo><t>0</t><v>{tempo_v}</v></tempo>\n'
            '\t</masterTrack>\n'
            + track_xml
            + '\t<monoTrack>\n'
            '\t</monoTrack>\n'
            '\t<stTrack>\n'
            '\t</stTrack>\n'
            '\t<aux>\n'
            '\t\t<id><![CDATA[AUX_VST_HOST_CHUNK_INFO]]></id>\n'
            '\t\t<content>'
            '<![CDATA[VlNDSwAAAAADAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=]]>'
            '</content>\n'
            '\t</aux>\n'
            '</vsq4>\n'
        )
        return xml

    # ----------------------------
    # VSQX 多音轨生成（对话文本框批量处理功能专用）
    # ----------------------------
    def build_vsqx_project_text_multitrack(
        self,
        project_title: str,
        tracks: List[Dict],
        config: AudioProcessingConfig,
        vsqx_singer: str = "MIKU_V4_Chinese",
        vsqx_singer_id: str = "BNGE7CP7EMTRSNC3",
        vsqx_singer_bs: int = 4,
        word_phoneme_map: bool = False,
        language: str = "",
        dict_source: str = "default",
    ) -> str:
        """
        生成包含多条音轨的 VSQX 工程文件（对话文本框批量处理功能）：
        每个对话框对应一条独立 <vsTrack>，全部写入*同一个* .vsqx 工程文件。

        Parameters
        ----------
        project_title : 工程文件标题（写入 masterTrack.seqName）
        tracks : 每个元素描述一条音轨：
            {
                "title": str,                            # 音轨名称（对话框文本/文件名）
                "segments": List[LabelSegment],
                "f0": Optional[np.ndarray],
                "t": Optional[np.ndarray],
                "native_english_words": Optional[set],
                "midi_notes": Optional[List],             # 该音轨若导入了 MIDI 则提供，
                                                            # 用于按 MIDI 原始音高设置 tone
            }
        config : 全局共享的音频处理配置（对话文本框页面顶部"高级设置"统一生效）
        vsqx_singer / vsqx_singer_id / vsqx_singer_bs : 全部音轨共用同一个声库
            （与单文件处理页面一致，本功能不支持逐音轨指定不同声库）
        word_phoneme_map / language / dict_source : 全局共享的单词→音素映射设置

        Returns
        -------
        str : VSQX XML 文本，包含 len(tracks) 条 <vsTrack> 与对应数量的
              mixer.vsUnit（tNo 与 vsTrack 一一对应，从 0 开始）
        """
        bpm = float(config.bpm)
        tempo_v = int(round(bpm * 100))

        track_blocks = ""
        vs_unit_blocks = ""

        for idx, tr in enumerate(tracks):
            tr_title = tr.get("title") or f"Track {idx + 1}"

            track_blocks += self._build_vsqx_track_block(
                tNo=idx,
                name=tr_title,
                segments=tr.get("segments") or [],
                f0=tr.get("f0"),
                t=tr.get("t"),
                config=config,
                midi_notes=tr.get("midi_notes"),
                vsqx_singer_bs=vsqx_singer_bs,
                word_phoneme_map=word_phoneme_map,
                language=language,
                native_english_words=tr.get("native_english_words"),
                dict_source=dict_source,
            )
            vs_unit_blocks += self._vsqx_vs_unit_block(idx)

        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
            '<vsq4 xmlns="http://www.yamaha.co.jp/vocaloid/schema/vsq4/"\n'
            '      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            '      xsi:schemaLocation="http://www.yamaha.co.jp/vocaloid/schema/vsq4/'
            ' vsq4.xsd">\n'
            '\t<vender><![CDATA[Yamaha corporation]]></vender>\n'
            '\t<version><![CDATA[4.0.0.3]]></version>\n'
            '\t<vVoiceTable>\n'
            '\t\t<vVoice>\n'
            f'\t\t\t<bs>{vsqx_singer_bs}</bs>\n'
            '\t\t\t<pc>0</pc>\n'
            f'\t\t\t<id><![CDATA[{vsqx_singer_id}]]></id>\n'
            f'\t\t\t<name><![CDATA[{vsqx_singer}]]></name>\n'
            '\t\t\t<vPrm>\n'
            '\t\t\t\t<bre>0</bre>\n'
            '\t\t\t\t<bri>0</bri>\n'
            '\t\t\t\t<cle>0</cle>\n'
            '\t\t\t\t<gen>0</gen>\n'
            '\t\t\t\t<ope>0</ope>\n'
            '\t\t\t</vPrm>\n'
            '\t\t</vVoice>\n'
            '\t</vVoiceTable>\n'
            '\t<mixer>\n'
            '\t\t<masterUnit>\n'
            '\t\t\t<oDev>0</oDev>\n'
            '\t\t\t<rLvl>0</rLvl>\n'
            '\t\t\t<vol>0</vol>\n'
            '\t\t</masterUnit>\n'
            + vs_unit_blocks
            + '\t\t<monoUnit>\n'
            '\t\t\t<iGin>0</iGin>\n'
            '\t\t\t<sLvl>-898</sLvl>\n'
            '\t\t\t<sEnable>0</sEnable>\n'
            '\t\t\t<m>0</m>\n'
            '\t\t\t<s>0</s>\n'
            '\t\t\t<pan>64</pan>\n'
            '\t\t\t<vol>0</vol>\n'
            '\t\t</monoUnit>\n'
            '\t\t<stUnit>\n'
            '\t\t\t<iGin>0</iGin>\n'
            '\t\t\t<m>0</m>\n'
            '\t\t\t<s>0</s>\n'
            '\t\t\t<vol>-129</vol>\n'
            '\t\t</stUnit>\n'
            '\t</mixer>\n'
            '\t<masterTrack>\n'
            f'\t\t<seqName><![CDATA[{project_title}]]></seqName>\n'
            '\t\t<comment><![CDATA[New VSQ File]]></comment>\n'
            '\t\t<resolution>480</resolution>\n'
            '\t\t<preMeasure>1</preMeasure>\n'
            '\t\t<timeSig><m>0</m><nu>4</nu><de>4</de></timeSig>\n'
            f'\t\t<tempo><t>0</t><v>{tempo_v}</v></tempo>\n'
            '\t</masterTrack>\n'
            + track_blocks
            + '\t<monoTrack>\n'
            '\t</monoTrack>\n'
            '\t<stTrack>\n'
            '\t</stTrack>\n'
            '\t<aux>\n'
            '\t\t<id><![CDATA[AUX_VST_HOST_CHUNK_INFO]]></id>\n'
            '\t\t<content>'
            '<![CDATA[VlNDSwAAAAADAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=]]>'
            '</content>\n'
            '\t</aux>\n'
            '</vsq4>\n'
        )
        return xml

    def _build_midi_output(
        self,
        output_path: str,
        segments: List[LabelSegment],
        config: AudioProcessingConfig,
        f0: Optional[np.ndarray] = None,
        t: Optional[np.ndarray] = None,
        midi_notes: Optional[List] = None,
    ) -> None:
        """
        将段落列表写成 MIDI 文件。

        音高优先级（与 SVP/USTX 生成保持一致）：
          1. MIDI 导入音符（midi_notes 覆盖）
          2. F0 中位音高（refine_pitch=True 时）
          3. config.base_pitch 默认值
        """
        from midi_processor import build_midi_from_segments, map_segment_to_midi_pitch

        note_data: List[Tuple[float, float, int, str]] = []
        base_tone = int(config.base_pitch)

        for seg in segments:
            if self._is_true_silence(seg.label):
                continue
            if seg.end_time <= seg.start_time:
                continue

            start_sec = self._lab_time_to_seconds(seg.start_time)
            end_sec   = self._lab_time_to_seconds(seg.end_time)

            tone = base_tone
            if midi_notes is not None:
                tone = map_segment_to_midi_pitch(
                    start_sec, end_sec, midi_notes,
                    base_pitch=base_tone,
                )
            elif config.refine_pitch and f0 is not None and t is not None and len(f0) > 0:
                mask = (t >= start_sec) & (t < end_sec)
                voiced = f0[mask]
                voiced = voiced[voiced > 0]
                if len(voiced) > 0:
                    midi_vals = 69.0 + 12.0 * np.log2(voiced / 440.0)
                    tone = int(round(float(np.median(midi_vals))))
                    tone = max(12, min(127, tone))

            note_data.append((start_sec, end_sec, tone, seg.label))

        build_midi_from_segments(
            segments=note_data,
            bpm=config.bpm,
            output_path=output_path,
        )


def _f0_worker(audio_path: str, config_dict: dict, q: mp.Queue) -> None:
    """
    子进程里做 F0 提取。
    将所有危险的 C++ PyWORLD 运算（包括分块和内存连续化）隔离在独立进程中。
    即使底层发生了 Access Violation (0xC0000005)，主进程也不会闪退！
    """
    try:
        import numpy as np
        import soundfile as sf
        try:
            import pyworld as pw
        except ImportError:
            q.put({"success": False, "error": "PyWORLD 未安装"})
            return

        x, sr = sf.read(audio_path)
        if len(x.shape) > 1:
            x = np.mean(x, axis=1)

        # 终极防崩溃：彻底清洗内存，转化为 C语言连续的 float64 数组
        x = np.nan_to_num(x, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        x = np.ascontiguousarray(x, dtype=np.float64)

        total_samples = len(x)
        duration_sec = total_samples / sr

        frame_period = 5.0
        if duration_sec > 600:
            frame_period = 10.0
        if duration_sec > 3600:
            frame_period = 20.0

        chunk_length_sec = 300
        chunk_size = int(chunk_length_sec * sr)

        f0_method = config_dict.get("f0_method", "dio").lower()
        f0_floor = config_dict.get("f0_floor", 71.0)
        f0_ceil = config_dict.get("f0_ceil", 800.0)

        def _extract_block(x_block: np.ndarray):
            if f0_method == "harvest":
                f0_b, t_b = pw.harvest(x_block, sr, f0_floor=f0_floor, f0_ceil=f0_ceil, frame_period=frame_period)
            else:
                f0_b, t_b = pw.dio(x_block, sr, f0_floor=f0_floor, f0_ceil=f0_ceil, frame_period=frame_period)
                f0_b = pw.stonemask(x_block, f0_b, t_b, sr)
            
            f0_b = np.asarray(f0_b, dtype=np.float64)
            f0_b[~np.isfinite(f0_b)] = 0.0
            # 使用原生的阈值切除假阳性高频
            f0_b[(f0_b < f0_floor * 0.6) | (f0_b > f0_ceil * 0.95)] = 0.0
            return f0_b, t_b

        if total_samples <= chunk_size:
            f0, t = _extract_block(x)
        else:
            f0_list = []
            t_list = []
            for start_idx in range(0, total_samples, chunk_size):
                end_idx = min(start_idx + chunk_size, total_samples)
                x_chunk = x[start_idx:end_idx]
                if len(x_chunk) < 256: 
                    continue
                f0_c, t_c = _extract_block(x_chunk)
                t_c = t_c + (start_idx / sr)
                f0_list.append(f0_c)
                t_list.append(t_c)

            f0 = np.concatenate(f0_list)
            t = np.concatenate(t_list)

        # 仅将干净的数据送出队列，避免在主进程做危险计算
        q.put({"success": True, "f0": f0, "t": t, "sr": sr})

    except Exception as e:
        import traceback
        q.put({"success": False, "error": f"{e}\n{traceback.format_exc()}"})
