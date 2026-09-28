# subtitle_processor.py
# -*- coding: utf-8 -*-
"""
字幕识别（视频/音频 → 逐句字幕）处理模块。

与现有 MFA / 对齐管线完全解耦，独立成模块，避免影响 pipeline.py /
alt_aligners.py 里已经跑通的强制对齐流程。核心思路：

  1) 若输入是视频，用 ffmpeg 抽取音轨为 16k 单声道 WAV（Qwen3-ASR 推荐采样率）。
  2) 用能量阈值法做 VAD 静音检测，把整段音频切成若干"语音块"
     （块之间是静音间隙）——切点必然落在真实停顿处，不会切断字词。
     这一步只负责"在哪里必须切"，不关心块有多长。
  3) 逐块调用本地加载的 Qwen3-ASR 模型做识别（return_time_stamps=True，
     拿到块内逐字/逐词时间戳），块的起止时间就是该句字幕的时间轴。
  4) 若某一块识别出的文本过长（超过阈值，一屏放不下），在标点符号处
     寻找离中点最近的切分点，结合块内已有的字级时间戳拆成两条子字幕，
     递归处理直至每条字幕长度合理——这一步只负责"长句要不要再切"。
  5) 过短、且与下一块间隔很近的碎片字幕允许合并，减少大量一两个字的
     无意义分行。
  6) 提供 SRT / LRC / TXT 三种纯文本导出。

VAD 打底 + 标点二次拆分的混合策略，兼顾"停顿处必切"（不切断语义）和
"长句必拆"（保证单条字幕可读），是主流字幕软件的常见做法。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# ffmpeg 探测
# ─────────────────────────────────────────────────────────────────────────

_FFMPEG_CACHE: Dict[str, Optional[str]] = {"ffmpeg": None, "ffprobe": None, "checked": None}


def _find_executable(name: str) -> Optional[str]:
    """
    在 PATH 中查找可执行文件；Windows 下 shutil.which 会自动尝试
    PATHEXT（.exe/.cmd 等），无需额外处理。
    """
    found = shutil.which(name)
    if found:
        return found
    # 常见 Windows 打包场景：ffmpeg 与本程序放在同一目录下的 ffmpeg/bin
    candidates = [
        Path(name),
        Path(f"{name}.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c.resolve())
    return None


def check_ffmpeg_available() -> Tuple[bool, str]:
    """
    探测 ffmpeg / ffprobe 是否可用。结果做简单缓存，避免每次请求都拉起
    子进程探测（探测本身很快，但字幕页面可能频繁轮询状态）。
    """
    ffmpeg_path = _find_executable("ffmpeg")
    ffprobe_path = _find_executable("ffprobe")
    _FFMPEG_CACHE["ffmpeg"] = ffmpeg_path
    _FFMPEG_CACHE["ffprobe"] = ffprobe_path

    if not ffmpeg_path:
        return False, "未检测到 ffmpeg，请安装后加入系统 PATH（视频转音频 / 音频重采样需要它）"
    if not ffprobe_path:
        return False, "未检测到 ffprobe（通常随 ffmpeg 一同安装），请确认安装完整"
    return True, f"ffmpeg: {ffmpeg_path}"


def get_ffmpeg_path() -> str:
    if not _FFMPEG_CACHE.get("ffmpeg"):
        check_ffmpeg_available()
    path = _FFMPEG_CACHE.get("ffmpeg")
    if not path:
        raise RuntimeError("未找到 ffmpeg，请安装并加入系统 PATH")
    return path


def get_ffprobe_path() -> str:
    if not _FFMPEG_CACHE.get("ffprobe"):
        check_ffmpeg_available()
    path = _FFMPEG_CACHE.get("ffprobe")
    if not path:
        raise RuntimeError("未找到 ffprobe，请安装并加入系统 PATH")
    return path


VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".ts", ".m4v"}
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wma", ".opus"}


def is_video_file(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTS


def probe_duration_sec(path: str) -> float:
    """用 ffprobe 读取媒体总时长（秒）。"""
    ffprobe = get_ffprobe_path()
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 读取时长失败: {result.stderr.strip()}")
    data = json.loads(result.stdout or "{}")
    duration = data.get("format", {}).get("duration")
    if duration is None:
        raise RuntimeError("ffprobe 未返回有效时长")
    return float(duration)


def extract_audio(src_path: str, dst_wav_path: str, sample_rate: int = 16000) -> str:
    """
    从视频/音频文件中提取单声道 16kHz WAV（Qwen3-ASR 推荐输入格式）。
    对纯音频输入同样适用（统一重采样/转声道，避免原始格式差异导致的
    识别质量波动）。
    """
    ffmpeg = get_ffmpeg_path()
    Path(dst_wav_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y",
        "-i", str(src_path),
        "-vn",                      # 丢弃视频流
        "-ac", "1",                 # 单声道
        "-ar", str(sample_rate),    # 采样率
        "-c:a", "pcm_s16le",
        str(dst_wav_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 提取音频失败: {result.stderr.strip()[-800:]}")
    if not Path(dst_wav_path).exists():
        raise RuntimeError("ffmpeg 执行完成但未生成输出文件")
    return dst_wav_path


# ─────────────────────────────────────────────────────────────────────────
# VAD 静音切分（第一层：按语音停顿硬切，保证不切断字词）
# ─────────────────────────────────────────────────────────────────────────

def _compute_rms_curve(audio, sr: int, frame_sec: float = 0.05, hop_sec: float = 0.02):
    """
    短时 RMS 能量曲线，向量化实现避免逐帧 Python 循环拖慢长音频。
    返回 (rms 数组, hop_sec)，rms[i] 对应时间 i * hop_sec 秒。
    """
    import numpy as np

    frame_n = max(1, int(round(frame_sec * sr)))
    hop_n = max(1, int(round(hop_sec * sr)))

    audio64 = np.asarray(audio, dtype=np.float64)
    if audio64.size == 0:
        return np.zeros(1, dtype=np.float64), hop_sec

    power = audio64 * audio64
    kernel = np.ones(frame_n, dtype=np.float64) / frame_n
    smoothed = np.convolve(power, kernel, mode="same")
    rms_full = np.sqrt(np.maximum(smoothed, 0.0))
    rms = rms_full[::hop_n]
    if rms.size == 0:
        rms = np.array([float(np.sqrt(np.mean(power)))], dtype=np.float64)
    return rms, hop_sec


def vad_split_segments(
    wav_path: str,
    min_silence_sec: float = 0.45,
    min_speech_sec: float = 0.25,
    max_speech_sec: float = 18.0,
    rel_threshold: float = 0.08,
    abs_floor: float = 0.0006,
    abs_ceiling: float = 0.006,
    padding_sec: float = 0.08,
) -> List[Tuple[float, float]]:
    """
    对整段音频做能量阈值 VAD，返回若干"语音块" [(start_sec, end_sec), ...]。

    策略：
      - 自适应阈值 = clip(rel_threshold × 全曲 70 分位能量, abs_floor, abs_ceiling)，
        用于把每一帧标记为"有声/静音"。
      - 连续静音时长 >= min_silence_sec 才被视为真正的句间停顿（短暂的
        辅音闭塞不会被误判为停顿）。
      - 静音之间的有声区间即为一个语音块；短于 min_speech_sec 的极短
        噪声块会被丢弃（不生成空字幕）。
      - 单个语音块超过 max_speech_sec（说话人几乎不停顿）时，在块内
        找一个局部能量低谷强制切开，避免出现极长的"一句话"。
      - 每个块两端各留 padding_sec 的余量（不越过相邻块边界），避免咬字
        的头尾被切掉。

    Returns
    -------
    按时间顺序排列、互不重叠的语音块列表；输入为静音或空音频时返回 []。
    """
    import numpy as np
    import soundfile as sf

    audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    total_sec = len(audio) / float(sr)
    if total_sec <= 0:
        return []

    rms, hop_sec = _compute_rms_curve(audio, sr)
    n_frames = len(rms)

    voiced_level = float(np.percentile(rms, 70)) if n_frames else 0.0
    threshold = max(abs_floor, min(rel_threshold * voiced_level, abs_ceiling))
    is_voiced = rms > threshold

    min_silence_frames = max(1, int(round(min_silence_sec / hop_sec)))

    # 找出所有"有声区间"：先找连续 True 的 run，再把间隔小于
    # min_silence_frames 的静音缝隙吸收进相邻有声区间（即短促停顿不切分）。
    runs: List[List[int]] = []  # [start_frame, end_frame) 的有声 run 列表
    i = 0
    while i < n_frames:
        if not is_voiced[i]:
            i += 1
            continue
        j = i
        while j < n_frames and is_voiced[j]:
            j += 1
        runs.append([i, j])
        i = j

    if not runs:
        return []

    merged: List[List[int]] = [runs[0]]
    for run in runs[1:]:
        prev = merged[-1]
        gap = run[0] - prev[1]
        if gap < min_silence_frames:
            prev[1] = run[1]
        else:
            merged.append(run)

    segments: List[Tuple[float, float]] = []
    for start_f, end_f in merged:
        start_sec = max(0.0, start_f * hop_sec - padding_sec)
        end_sec = min(total_sec, end_f * hop_sec + padding_sec)
        if end_sec - start_sec >= min_speech_sec:
            segments.append((start_sec, end_sec))

    # 相邻块之间留出的 padding 可能导致重叠，做一次夹紧处理。
    for k in range(1, len(segments)):
        prev_start, prev_end = segments[k - 1]
        cur_start, cur_end = segments[k]
        if cur_start < prev_end:
            mid = (prev_end + cur_start) / 2.0
            segments[k - 1] = (prev_start, mid)
            segments[k] = (mid, cur_end)

    # 超长语音块（长时间无停顿）在内部能量低谷处强制二次切分。
    final_segments: List[Tuple[float, float]] = []
    for start_sec, end_sec in segments:
        final_segments.extend(
            _force_split_long_segment(rms, hop_sec, start_sec, end_sec, max_speech_sec)
        )

    return final_segments


def _force_split_long_segment(
    rms, hop_sec: float, start_sec: float, end_sec: float, max_speech_sec: float
) -> List[Tuple[float, float]]:
    """对超过 max_speech_sec 的语音块，在中段能量最低点强制切一刀（递归）。"""
    import numpy as np

    duration = end_sec - start_sec
    if duration <= max_speech_sec:
        return [(start_sec, end_sec)]

    # 在 [35%, 65%] 区间内找能量最低的一帧作为切点，避免切到开头/结尾。
    search_lo = start_sec + duration * 0.35
    search_hi = start_sec + duration * 0.65
    lo_frame = int(search_lo / hop_sec)
    hi_frame = max(lo_frame + 1, int(search_hi / hop_sec))
    lo_frame = max(0, min(lo_frame, len(rms) - 1))
    hi_frame = max(0, min(hi_frame, len(rms)))

    if hi_frame <= lo_frame:
        mid_sec = start_sec + duration / 2.0
    else:
        window = np.asarray(rms[lo_frame:hi_frame])
        split_frame = lo_frame + int(np.argmin(window))
        mid_sec = split_frame * hop_sec

    left = _force_split_long_segment(rms, hop_sec, start_sec, mid_sec, max_speech_sec)
    right = _force_split_long_segment(rms, hop_sec, mid_sec, end_sec, max_speech_sec)
    return left + right


# ─────────────────────────────────────────────────────────────────────────
# 长句二次拆分（第二层：按标点 + 字级时间戳把过长的一句拆成多条字幕）
# ─────────────────────────────────────────────────────────────────────────

_SENTENCE_PUNCT = "。！？；\n" + ".!?;"
_CLAUSE_PUNCT = "，、,"

MAX_SUBTITLE_CHARS = 34       # 单条字幕建议最大字符数（超过考虑二次拆分）
MIN_SUBTITLE_CHARS = 1        # 允许的最短字符数（配合合并逻辑处理碎片）

# "移除符号"开关用到的标点集合：比切分用的 _SENTENCE_PUNCT/_CLAUSE_PUNCT
# 范围更全，覆盖中英文常见标点（引号、括号、破折号、省略号等），因为这里
# 目的是"字幕文本里不要出现标点"，而不是"找切分点"，所以需要的字符集
# 更宽。特意不包含空格——移除标点后英文单词之间的空格还需要保留。
_REMOVABLE_PUNCT = set(
    "。！？；，、,.!?;:：""''\"'「」『』（）()【】[]《》<>—–-…～~·•*#@%^&_=+|\\/"
)


def strip_punctuation(text: str) -> str:
    """
    "移除符号"开关用：去掉文本里的标点符号。每个被移除的标点原地替换成
    一个空格（而不是直接吞掉），这样标点原本分隔语义的作用还在——比如
    "你好，世界" 变成"你好 世界"而不是"你好世界"，避免看起来像被误拼成
    了一个词。多个标点/空白相邻产生的连续空格再合并为一个，首尾空白
    strip 掉。仅用于字幕文本本身，不触碰时间轴。
    """
    if not text:
        return text
    cleaned = "".join(" " if ch in _REMOVABLE_PUNCT else ch for ch in text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _char_time_from_segments(
    text: str, time_stamps: List[List[Optional[float]]], block_start: float, block_end: float
) -> List[Tuple[str, float, float]]:
    """
    把 Qwen3-ASR 返回的 (text, time_stamps) 归一化为
    [(char_or_token, start_sec, end_sec), ...]，坐标已经加上 block_start
    偏移（Qwen3-ASR 返回的是相对该次调用音频片段起点的时间）。

    time_stamps 长度可能与 text 不完全一致（标点、静音符号等不一定有
    对应时间戳），此处做尽力而为的对齐：数量一致按逐字符对应，否则退化
    为整块只有一个时间跨度。
    """
    n = len(text)
    if n == 0:
        return []

    if time_stamps and len(time_stamps) == n:
        out = []
        for ch, ts in zip(text, time_stamps):
            s, e = (ts + [None, None])[:2]
            if s is None or e is None:
                continue
            out.append((ch, block_start + float(s), block_start + float(e)))
        if out:
            return out

    # 时间戳数量对不上文本长度：按字符数量把整块时长均分（保底方案，
    # 仍然保证每个字符都有一个合理的时间区间，不影响后续切分逻辑）。
    dur = max(block_end - block_start, 0.01)
    per_char = dur / n
    return [
        (ch, block_start + i * per_char, block_start + (i + 1) * per_char)
        for i, ch in enumerate(text)
    ]


def _find_split_index(text: str, allow_comma_split: bool = False) -> Optional[int]:
    """
    在 text 中寻找一个尽量靠近中点、且落在标点后面的切分下标（切分点之前
    的内容归入前半句，含标点本身）。只找"一刀"，不是"每个标点都切"——
    用于两个场景：① _split_by_length 在某一段仍超过 max_chars 时找最靠
    近中点的标点位置下刀；② split_entry_manually 用户手动拆一条字幕成
    两条。"无条件在每个标点处都切开一条新字幕"的场景请用
    _split_at_every_punct，不经过这个函数。

    allow_comma_split=False（默认）时只认句末标点（。！？；等），找不到
    就直接返回 None，交给调用方按字数硬切。

    allow_comma_split=True 时，句末标点和逗号/顿号等次级标点同等优先级
    考虑：把两者的候选下标放在一起挑离中点最近的那个。
    """
    n = len(text)
    mid = n / 2.0

    def _candidates(punct_set: str) -> List[int]:
        idxs = [i for i, ch in enumerate(text) if ch in punct_set]
        # 标点后面切；候选下标转换为"切分点"（该标点之后的位置）
        return [i + 1 for i in idxs if i + 1 < n]

    if allow_comma_split:
        candidates = _candidates(_SENTENCE_PUNCT) + _candidates(_CLAUSE_PUNCT)
        if candidates:
            return min(candidates, key=lambda i: abs(i - mid))
        return None

    candidates = _candidates(_SENTENCE_PUNCT)
    if candidates:
        return min(candidates, key=lambda i: abs(i - mid))
    return None


def _split_at_every_punct(text: str, punct_set: str) -> List[str]:
    """
    把 text 在每一个属于 punct_set 的标点处都切开（不只是找离中点最近的
    那一个），标点本身归入它前面的那一段。例如 punct_set=_SENTENCE_PUNCT
    时，"大家好，你好。真棒！" 会被切成 ["大家好，你好。", "真棒！"]；
    punct_set=_SENTENCE_PUNCT + _CLAUSE_PUNCT 时会连逗号/顿号也切开，
    变成 ["大家好，", "你好。", "真棒！"]。

    连续标点（如"……""，。"）不会产生空段；文本结尾的标点不会切出一个
    空的尾段。
    """
    if not text:
        return []
    pieces: List[str] = []
    start = 0
    n = len(text)
    for i, ch in enumerate(text):
        if ch in punct_set:
            piece = text[start:i + 1]
            if piece:
                pieces.append(piece)
            start = i + 1
    if start < n:
        pieces.append(text[start:])
    return pieces


def _split_long_entry(
    text: str,
    char_times: List[Tuple[str, float, float]],
    max_chars: int,
    allow_comma_split: bool = False,
    split_at_sentence_end: bool = False,
) -> List[Tuple[str, float, float]]:
    """
    把一条字幕拆成多条，每条形如 (text, start_sec, end_sec)。
    char_times 与 text 等长，逐字符对应时间。

    split_at_sentence_end=True 时，行为不再是"只有超过 max_chars 才
    尝试在标点处切"——而是无条件在每一个句末标点（。！？；等）处都切开
    一条新字幕，与长度无关，这是用户主动要的"遇到句号就换一条字幕"
    效果。此时 allow_comma_split 才有意义：为 True 则连逗号/顿号也
    一并当作切分点（"大家好，你好。" → "大家好，" / "你好。"）；为
    False（默认）则只在句末标点处切，逗号不参与。切开之后，如果某一段
    仍然超过 max_chars（比如两个标点之间的内容本身就很长），再对这一段
    递归地按字数在中点附近硬切，保证单条字幕不会失控地长。

    split_at_sentence_end=False（默认）时保留原来的行为：只有整段超过
    max_chars 才二次拆分，只在句末标点处切，找不到就按字数硬切；
    allow_comma_split 在这一分支下被忽略（逗号完全不参与）。
    """
    if not text or not char_times:
        return []

    if split_at_sentence_end:
        punct_set = _SENTENCE_PUNCT + _CLAUSE_PUNCT if allow_comma_split else _SENTENCE_PUNCT
        pieces = _split_at_every_punct(text, punct_set)
        if len(pieces) <= 1:
            # 没有任何标点可切：退化为按长度硬切（沿用原逻辑）
            return _split_by_length(text, char_times, max_chars)

        out: List[Tuple[str, float, float]] = []
        offset = 0
        for piece in pieces:
            piece_len = len(piece)
            piece_times = char_times[offset:offset + piece_len]
            offset += piece_len
            if not piece_times:
                continue
            out.extend(_split_by_length(piece, piece_times, max_chars))
        return out

    return _split_by_length(text, char_times, max_chars, allow_comma_split=False)


def _split_by_length(
    text: str,
    char_times: List[Tuple[str, float, float]],
    max_chars: int,
    allow_comma_split: bool = False,
) -> List[Tuple[str, float, float]]:
    """
    原 _split_long_entry 的"超长才切"逻辑：只有 text 超过 max_chars 才
    在标点处（或找不到标点时按中点）递归二次拆分。allow_comma_split 在
    这里只影响"找不到句末标点时是否退化尝试逗号"，用法和之前一致；
    由 _split_long_entry 的逗号全切分支调用时永远传 False，因为逗号已经
    在上一层切过了，这里只需要处理"单段仍然超长"的情况。
    """
    if len(text) <= max_chars or len(text) <= 1:
        if not char_times:
            return []
        return [(text, char_times[0][1], char_times[-1][2])]

    split_idx = _find_split_index(text, allow_comma_split=allow_comma_split)
    if split_idx is None or split_idx <= 0 or split_idx >= len(text):
        # 找不到合适标点：退化为在中点附近按长度硬切（尽量不切在标点内）
        split_idx = max(1, len(text) // 2)

    left_text, right_text = text[:split_idx], text[split_idx:]
    left_times, right_times = char_times[:split_idx], char_times[split_idx:]

    left_entries = _split_by_length(left_text, left_times, max_chars, allow_comma_split)
    right_entries = _split_by_length(right_text, right_times, max_chars, allow_comma_split)
    return left_entries + right_entries


# ─────────────────────────────────────────────────────────────────────────
# 主流程：调用本地 Qwen3-ASR 逐块识别 → 组装字幕条目
# ─────────────────────────────────────────────────────────────────────────

class SubtitleEntry:
    __slots__ = ("index", "start", "end", "text")

    def __init__(self, index: int, start: float, end: float, text: str):
        self.index = index
        self.start = start
        self.end = end
        self.text = text

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "start": self.start, "end": self.end, "text": self.text}


def _slice_wav(wav_path: str, start_sec: float, end_sec: float, out_path: str) -> str:
    """用 ffmpeg 从整段 WAV 中切出 [start_sec, end_sec] 片段，供逐块识别。"""
    ffmpeg = get_ffmpeg_path()
    duration = max(end_sec - start_sec, 0.02)
    cmd = [
        ffmpeg, "-y",
        "-ss", f"{start_sec:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(wav_path),
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 切片失败: {result.stderr.strip()[-500:]}")
    return out_path


_QWEN3_LANG_MAP = {
    "auto": None,
    "zh": "Chinese", "yue": "Cantonese", "en": "English", "ar": "Arabic",
    "de": "German", "fr": "French", "es": "Spanish", "pt": "Portuguese",
    "id": "Indonesian", "it": "Italian", "ko": "Korean", "ru": "Russian",
    "th": "Thai", "vi": "Vietnamese", "ja": "Japanese", "tr": "Turkish",
    "hi": "Hindi", "ms": "Malay", "nl": "Dutch", "sv": "Swedish",
    "da": "Danish", "fi": "Finnish", "pl": "Polish", "cs": "Czech",
    "fil": "Filipino", "fa": "Persian", "el": "Greek", "hu": "Hungarian",
    "mk": "Macedonian", "ro": "Romanian",
}


def resolve_qwen3_language(lang_code: str) -> Optional[str]:
    return _QWEN3_LANG_MAP.get((lang_code or "auto").lower(), None)


def _transcribe_to_subtitles_impl(
    wav_path: str,
    language: str = "auto",
    device: str = "auto",
    max_chars: int = MAX_SUBTITLE_CHARS,
    tmp_dir: Optional[str] = None,
    progress_cb=None,
    allow_comma_split: bool = False,
    split_at_sentence_end: bool = False,
    remove_punctuation: bool = False,
    close_vad_gaps: bool = False,
    vad_gap_threshold_sec: float = 0.6,
    batch_size: int = 8,
) -> List[SubtitleEntry]:
    """
    对完整 WAV 做 VAD 切句 → 逐块 ASR 识别 → 长句二次拆分，返回按时间
    排序的字幕条目列表。

    progress_cb(done, total) 可选，用于上报进度给调用方（Flask job）。
    split_at_sentence_end：是否允许按句末切分（默认 False）。开启后不是
        "超过 max_chars 才尝试"，而是无条件遇到句末标点（。！？；等）
        就切成下一条字幕，与长度无关。关闭时只有整段超过 max_chars 才
        会在句末标点处二次拆分，找不到就按字数硬切。
    allow_comma_split：是否把逗号/顿号也当作切分点（默认 False，只在
        句末标点处切）。仅在 split_at_sentence_end=True 时才有意义——
        开启后连逗号/顿号也会切成下一条字幕，例如"大家好，你好。"会
        变成两条："大家好，" 和 "你好。"；split_at_sentence_end=False
        时本参数被忽略。切开后若某一段本身仍然超过 max_chars，再对
        这一段按字数在中点附近继续硬切。切分用的是这一段音频块内逐字
        ASR 时间戳，精确到字。
    remove_punctuation：是否在识别结果落地前去掉标点符号（默认 False）。
        开启后编辑区显示的文本和后续导出的 SRT/LRC/TXT 都不含标点，
        原标点位置会保留一个空格。
    close_vad_gaps：VAD 合并间隔开关（默认 False）。开启后，相邻两条
        字幕之间只要静音间隔大于 vad_gap_threshold_sec，就把这段间隙
        对半分配到中点——前一条字幕的结束时间和后一条的开始时间都移到
        间隙中点，不论间隙原本有多长，都是直接对半分（不是收紧到贴合），
        让两条字幕的时间轴挨得更近。这不是把两条字幕的文本合并成一条，
        条目数量不变。
    vad_gap_threshold_sec：触发这一处理的间隔下限（秒），默认 0.6。
        间隔小于等于该值时视为已经足够紧凑，保持原样不动；大于该值
        才会被对半分配到中点。
    batch_size：透传给 alt_aligners._qwen3_load_asr_model() 的
        max_inference_batch_size（默认 8，与该接口自身默认值一致）。
        显存不足时可调小；显存充裕时调大可以提速。

    【2026-08 起】Qwen3-ASR 已迁入本进程（.mfa_env）内本地加载，这里不再
    通过 HTTP 调用独立的 qwen3_server.py，改为直接调用
    alt_aligners._qwen3_load_asr_model() 拿到（惰性加载、跨调用复用的）
    本地模型实例，逐块调用其 .transcribe()。显存不足时的自动降级（腰斩
    batch_size → 整体切 CPU）逻辑与 alt_aligners.Qwen3ASRAligner 保持
    一致，直接复用同一套模块级辅助函数，不再维护两份。
    """
    from alt_aligners import (
        _qwen3_load_asr_model,
        _qwen3_normalize_segments,
        _is_cuda_oom_or_env_error,
        _safe_device,
    )
    import alt_aligners as _alt_aligners_mod

    segments = vad_split_segments(wav_path)
    if not segments:
        return []

    asr_lang = resolve_qwen3_language(language)
    device_override = _safe_device(device)
    tmp_root = Path(tmp_dir) if tmp_dir else Path(wav_path).parent / f"_subtitle_chunks_{uuid.uuid4().hex[:8]}"
    tmp_root.mkdir(parents=True, exist_ok=True)

    entries: List[SubtitleEntry] = []
    total = len(segments)

    model = _qwen3_load_asr_model(device_override, batch_size)
    if model is None:
        raise RuntimeError("Qwen3-ASR 模型加载失败")

    try:
        for i, (seg_start, seg_end) in enumerate(segments):
            chunk_path = tmp_root / f"chunk_{i:05d}.wav"
            _slice_wav(wav_path, seg_start, seg_end, str(chunk_path))

            try:
                result = model.transcribe(
                    audio=str(chunk_path.resolve()),
                    language=asr_lang,
                    context="",
                    return_time_stamps=True,
                )
            except Exception as e:
                if not _is_cuda_oom_or_env_error(e):
                    raise
                logger.warning("字幕分块识别失败（第 %d 块，显存不足，自动降级重试）：%s", i, e)
                try:
                    import torch as _torch_oom
                    if _torch_oom.cuda.is_available():
                        _torch_oom.cuda.empty_cache()
                except Exception:
                    pass
                retried_model = None
                if _alt_aligners_mod._qwen3_asr_model_device != "cpu" and batch_size > 1:
                    retried_model = _qwen3_load_asr_model(device_override, max(1, batch_size // 2))
                if retried_model is None:
                    retried_model = _qwen3_load_asr_model("cpu", 1)
                if retried_model is None:
                    logger.warning("字幕分块识别失败（第 %d 块）：显存不足自动降级后模型仍加载失败", i)
                    if progress_cb:
                        progress_cb(i + 1, total)
                    continue
                model = retried_model
                try:
                    result = model.transcribe(
                        audio=str(chunk_path.resolve()),
                        language=asr_lang,
                        context="",
                        return_time_stamps=True,
                    )
                except Exception as e2:
                    logger.warning("字幕分块识别失败（第 %d 块）：%s", i, e2)
                    if progress_cb:
                        progress_cb(i + 1, total)
                    continue

            raw_segments = _qwen3_normalize_segments(result)
            block_text = "".join((s.get("text") or "") for s in raw_segments).strip()
            # 【新增】与 alt_aligners.Qwen3ASRAligner.align() 保持一致，
            # 把每块的识别结果打到控制台/日志，便于实时观察字幕识别进度
            # 与内容（此前这里只有 transformers 自身的 checkpoint/生成
            # 警告输出，识别出的文字完全没有落地到日志）。
            logger.info(
                "[Qwen3-ASR] 第 %d/%d 块识别文本: %s",
                i + 1, total, block_text[:120] if block_text else "(空)",
            )
            if not block_text:
                if progress_cb:
                    progress_cb(i + 1, total)
                continue

            # 合并所有子 segment 的时间戳（通常单块只返回一个 segment，
            # 但保留多 segment 的兼容处理）。
            merged_ts: List[List[Optional[float]]] = []
            for s in raw_segments:
                merged_ts.extend(s.get("time_stamps") or [])

            char_times = _char_time_from_segments(block_text, merged_ts, seg_start, seg_end)
            if not char_times:
                char_times = [(block_text, seg_start, seg_end)]  # 极端兜底

            split_entries = _split_long_entry(
                block_text, char_times, max_chars,
                allow_comma_split=allow_comma_split,
                split_at_sentence_end=split_at_sentence_end,
            ) if len(char_times) == len(block_text) else [(block_text, seg_start, seg_end)]

            for text, s, e in split_entries:
                cleaned = text.strip()
                if remove_punctuation:
                    cleaned = strip_punctuation(cleaned)
                if cleaned:
                    entries.append(SubtitleEntry(len(entries) + 1, s, e, cleaned))

            if progress_cb:
                progress_cb(i + 1, total)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    entries = _merge_short_fragments(entries)
    if close_vad_gaps:
        entries = _close_small_gaps(entries, min_gap_sec=vad_gap_threshold_sec)
    for idx, e in enumerate(entries, start=1):
        e.index = idx
    return entries


def transcribe_to_subtitles(
    wav_path: str,
    language: str = "auto",
    device: str = "auto",
    max_chars: int = MAX_SUBTITLE_CHARS,
    tmp_dir: Optional[str] = None,
    progress_cb=None,
    allow_comma_split: bool = False,
    split_at_sentence_end: bool = False,
    remove_punctuation: bool = False,
    close_vad_gaps: bool = False,
    vad_gap_threshold_sec: float = 0.6,
    batch_size: int = 8,
) -> List[SubtitleEntry]:
    """
    对外入口：参数与文档见 _transcribe_to_subtitles_impl()。

    【用完即卸】卸载必须放在 impl 返回之后。impl 内部的局部变量
    （model / retried_model / result 等）持有 Qwen3-ASR 模型（含其内嵌的
    forced_aligner 子模型）的强引用；如果在 impl 自己的 finally 里卸载，
    这些引用仍然存活，模型根本没有被回收，torch.cuda.empty_cache() 也就
    归还不了显存（表现为日志已打印"已按「用完即卸」设置释放模型"，任务
    管理器里显存占用却纹丝不动）。impl 返回后其栈帧被销毁，引用归零，
    此时再卸载才能真正把显存还给系统。
    """
    try:
        return _transcribe_to_subtitles_impl(
            wav_path,
            language=language,
            device=device,
            max_chars=max_chars,
            tmp_dir=tmp_dir,
            progress_cb=progress_cb,
            allow_comma_split=allow_comma_split,
            split_at_sentence_end=split_at_sentence_end,
            remove_punctuation=remove_punctuation,
            close_vad_gaps=close_vad_gaps,
            vad_gap_threshold_sec=vad_gap_threshold_sec,
            batch_size=batch_size,
        )
    finally:
        # 无论成功、失败还是中途异常都要卸载（失败任务同样应当放行显存）。
        try:
            from alt_aligners import _get_unload_after_task_settings, _qwen3_unload_asr_model
            if _get_unload_after_task_settings().get("unload_qwen3_asr_after_task"):
                _qwen3_unload_asr_model()
        except Exception as _unload_err:
            logger.warning(f"[Qwen3-ASR] 「用完即卸」释放模型失败（不影响本次识别结果）: {_unload_err}")


def _close_small_gaps(
    entries: List[SubtitleEntry], min_gap_sec: float = 0.6
) -> List[SubtitleEntry]:
    """
    VAD 合并间隔开关：不合并文本、不减少条目数，只把相邻两条字幕之间
    的静音间隙对半分配到中点——前一条的 end 推到间隙中点，后一条的
    start 拉到同一个中点，让两条字幕挨得很近。

    只有当间隔 > min_gap_sec 时才触发这个收紧；间隔 <= min_gap_sec 视为
    已经足够紧凑，保持原样不动。一旦触发，不论间隙本身有多长，都是
    直接对半分到中点（不是把间隙收紧到贴合）。
    """
    if len(entries) < 2:
        return entries
    for i in range(len(entries) - 1):
        cur = entries[i]
        nxt = entries[i + 1]
        gap = nxt.start - cur.end
        if gap > min_gap_sec:
            mid = (cur.end + nxt.start) / 2.0
            cur.end = mid
            nxt.start = mid
    return entries


def build_aligned_sentence_entries(
    parts: List[str],
    spans: Optional[List[Tuple[Optional[float], Optional[float]]]],
    duration: float,
    close_vad_gaps: bool = False,
    vad_gap_threshold_sec: float = 0.6,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    字幕对齐页：把"句子文本 + 每句的真实起止时间"组装成可编辑字幕条目。

    spans 来自 alt_aligners.sentence_spans_from_word_entries()：
      - 为 None：文本与对齐结果对不上，退回旧的"按字数占比均摊整段时长"
        （首尾相接、不含任何真实停顿），并在返回的 mode 里标记为 "proportional"，
        让调用方能给用户一个明确提示，而不是悄悄给出不准的结果。
      - 为列表：每句用自己首字起点/末字终点，句间真实的停顿（SIL）原样保留为
        相邻两条之间的空隙，mode 为 "aligned"。

    close_vad_gaps / vad_gap_threshold_sec 与字幕识别页完全同语义（复用
    _close_small_gaps）：相邻两条间隔 > 阈值时，把间隙对半分到中点，不合并
    文本、不减少条目数。仅在 mode == "aligned" 时才有意义——均摊结果本来就
    没有间隙可收紧。
    """
    n = len(parts)
    if n == 0:
        return [], "aligned"

    if spans is None:
        weights = [max(1, len(re.sub(r"\s+", "", p))) for p in parts]
        total = float(sum(weights)) or 1.0
        cursor = 0.0
        out: List[Dict[str, Any]] = []
        for i, part in enumerate(parts):
            end = duration if i == n - 1 else cursor + duration * weights[i] / total
            out.append({
                "start": round(cursor, 3),
                "end": round(max(end, cursor + 0.01), 3),
                "text": part,
            })
            cursor = end
        return out, "proportional"

    # 没有可发音单元的句子（纯标点）：start/end 借相邻句子的边界补齐，
    # 保证时间轴仍然单调、不出现 None。
    starts: List[Optional[float]] = [a for a, _ in spans]
    ends: List[Optional[float]] = [b for _, b in spans]
    for i in range(n):
        if starts[i] is None:
            # 起点 = 前一条的终点（没有前一条则 0）
            starts[i] = ends[i - 1] if i > 0 and ends[i - 1] is not None else 0.0
            # 终点 = 起点，先占位，下面统一扩成最小时长
            ends[i] = starts[i]

    entries: List[SubtitleEntry] = []
    prev_end = 0.0
    for i in range(n):
        st = max(float(starts[i]), prev_end)          # 不允许与前一条重叠
        en = max(float(ends[i]), st + 0.01)           # 至少 10ms，避免零时长
        if duration and duration > 0:
            en = min(en, max(duration, st + 0.01))    # 不超过媒体总时长
        entries.append(SubtitleEntry(i + 1, st, en, parts[i]))
        prev_end = en

    if close_vad_gaps:
        entries = _close_small_gaps(entries, min_gap_sec=vad_gap_threshold_sec)

    return [
        {"start": round(e.start, 3), "end": round(e.end, 3), "text": e.text}
        for e in entries
    ], "aligned"


def _merge_short_fragments(
    entries: List[SubtitleEntry], min_chars: int = 3, max_gap_sec: float = 0.6, max_merged_chars: int = MAX_SUBTITLE_CHARS
) -> List[SubtitleEntry]:
    """
    把过短（< min_chars）且与下一条间隔很近（< max_gap_sec）的碎片字幕
    并入下一条，减少满屏都是一两个字的无意义分行。合并后长度若会超过
    max_merged_chars 则不合并，保留原样（避免二次拆分逻辑白做）。
    """
    if not entries:
        return entries

    merged: List[SubtitleEntry] = []
    i = 0
    while i < len(entries):
        cur = entries[i]
        if (
            len(cur.text) < min_chars
            and i + 1 < len(entries)
            and entries[i + 1].start - cur.end < max_gap_sec
            and len(cur.text) + len(entries[i + 1].text) <= max_merged_chars
        ):
            nxt = entries[i + 1]
            combined_text = cur.text + nxt.text
            merged.append(SubtitleEntry(0, cur.start, nxt.end, combined_text))
            i += 2
        else:
            merged.append(cur)
            i += 1
    return merged


# ─────────────────────────────────────────────────────────────────────────
# 导出：SRT / LRC / TXT
# ─────────────────────────────────────────────────────────────────────────

def split_entry_manually(
    text: str, start: float, end: float, split_ratio: float = 0.5
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    手动把已经在编辑区里的一条字幕拆成两条（用户点了"拆分"按钮）。

    这条字幕此时可能已经被用户编辑过文本、合并过、或者根本是手动新建的
    一条空字幕，早就没有逐字时间戳了，所以不能像识别阶段的
    _split_long_entry 那样按字级时间戳切分——这里退化为两条更简单、但
    足够实用的规则：
      1) 文本切分点：优先找离"文本中点"最近的句末/逗号标点（复用
         _find_split_index，允许逗号兜底，因为这是用户主动要求拆分，
         没有"避免切太碎"的顾虑），找不到标点就按字符数中点硬切；
      2) 时间切分点：按"前半段文本长度 / 总文本长度"的比例，从
         [start, end] 区间里插值出一个切分时刻，前后两条字幕分别占用
         比例对应的时长——没有真实时间戳时，按文本长度分配时长是最合理
         的近似。

    split_ratio 保留给以后"按播放器当前光标位置拆分"之类的场景（例如
    前端可以算出光标落在文本的第几个字符，转换成 0~1 的比例传进来）；
    默认 0.5 表示按文本自动找中点附近的标点。

    返回 (left_dict, right_dict)，形如 {"start":..., "end":..., "text":...}。
    调用方需要自行处理"拆出来某一段为空文本"的情况（不追加空字幕）。
    """
    text = text or ""
    n = len(text)
    end = max(end, start + 0.02)

    if n <= 1:
        # 单字符或空文本无法再拆，退化为在时间上对半分，文本全部归左侧
        mid_time = (start + end) / 2.0
        return (
            {"start": start, "end": mid_time, "text": text},
            {"start": mid_time, "end": end, "text": ""},
        )

    # 文本切分点：允许逗号兜底（用户主动拆分，不需要像自动识别那样保守）
    split_idx = _find_split_index(text, allow_comma_split=True)
    if split_idx is None or split_idx <= 0 or split_idx >= n:
        split_idx = max(1, round(n * max(0.0, min(1.0, split_ratio))))
        split_idx = min(max(split_idx, 1), n - 1)

    left_text = text[:split_idx].strip()
    right_text = text[split_idx:].strip()

    # 时间切分点：按左右文本长度比例插值（用字符数而非切分下标本身，
    # 因为 strip() 掉的前后空白不应该占用时长）
    left_len = max(len(left_text), 1)
    right_len = max(len(right_text), 1)
    ratio = left_len / (left_len + right_len)
    duration = end - start
    split_time = start + duration * ratio
    # 保证两段都至少有一个最小可感知时长，避免出现零长度字幕
    min_seg = min(0.1, duration / 2.0)
    split_time = max(start + min_seg, min(split_time, end - min_seg))

    return (
        {"start": start, "end": split_time, "text": left_text},
        {"start": split_time, "end": end, "text": right_text},
    )


def _format_srt_time(sec: float) -> str:
    """
    将秒数格式化为 SRT 要求的 HH:MM:SS,mmm。

    直接从"总毫秒数"整数逐级取模拆分（毫秒 → 秒 → 分 → 时），而不是先对
    sec 取 // 3600 / % 60 等再单独四舍五入毫秒部分——后一种做法在临界值
    四舍五入进位时只把 +1 补到秒上，若秒本身已经是 59 会产出非法的
    "60 秒"（甚至连锁产生"60 分"），例如 59.9997 秒之前会被格式化成
    "00:00:60,000"。统一从毫秒开始逐级 divmod 可以让进位自然逐级传播，
    不会出现这类非法时间戳。
    """
    total_ms = round(max(0.0, sec) * 1000)
    total_ms, ms = divmod(total_ms, 1000)
    total_sec, secs = divmod(total_ms, 60)
    hours, minutes = divmod(total_sec, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _format_lrc_time(sec: float) -> str:
    """
    格式化为 LRC 要求的 [MM:SS.xx]。同样从"总厘秒数"整数逐级 divmod 拆分，
    避免 secs 四舍五入到 60.00 而不进位到分钟的问题（例如 59.999 秒）。
    """
    total_cs = round(max(0.0, sec) * 100)
    minutes, cs = divmod(total_cs, 6000)
    secs, cs = divmod(cs, 100)
    return f"[{minutes:02d}:{secs:02d}.{cs:02d}]"


def export_srt(entries: List[Dict[str, Any]]) -> str:
    lines = []
    for i, e in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_format_srt_time(e['start'])} --> {_format_srt_time(e['end'])}")
        lines.append(e["text"])
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def export_lrc(entries: List[Dict[str, Any]]) -> str:
    lines = []
    for e in entries:
        lines.append(f"{_format_lrc_time(e['start'])}{e['text']}")
    return "\n".join(lines) + "\n"


def export_txt(entries: List[Dict[str, Any]]) -> str:
    return "\n".join(e["text"] for e in entries) + "\n"


def _lab_safe_label(text: str) -> str:
    """
    LAB 每行是"起 止 标签"三个以空白分隔的字段（与本项目 MFA/强制对齐
    产出的 .lab 同一套约定，参见 tts_processor._entries_to_lab_text），
    标签字段本身不能包含空白或换行，否则会被解析成多余的字段。字幕文本
    常见含空格/换行/制表符，这里统一折叠成单个空格；折叠后为空则回退为
    下划线，避免产出空标签导致该行被下游解析器忽略。
    """
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    return collapsed or "_"


def export_lab(entries: List[Dict[str, Any]]) -> str:
    """
    导出为 LAB 格式：每条字幕一行 "<起始_100ns> <结束_100ns> <文本>"，
    与本项目强制对齐管线产出的 HTK 风格 .lab 时间单位一致（100ns，即
    1 秒 = 10,000,000 单位），方便直接复用现有 LAB 相关工具链。这里是
    "整句一行"的字幕级 LAB，不同于 MFA/Qwen3-FA 对齐产出的音素级 LAB。
    """
    lines = []
    for e in entries:
        start_100ns = round(max(0.0, float(e["start"])) * 10_000_000)
        end_100ns = round(max(0.0, float(e["end"])) * 10_000_000)
        if end_100ns <= start_100ns:
            continue
        lines.append(f"{start_100ns} {end_100ns} {_lab_safe_label(e['text'])}")
    return "\n".join(lines) + "\n"


EXPORTERS = {
    "srt": export_srt,
    "lrc": export_lrc,
    "txt": export_txt,
    "lab": export_lab,
}


def export_subtitles(entries: List[Dict[str, Any]], fmt: str) -> str:
    fn = EXPORTERS.get(fmt)
    if not fn:
        raise ValueError(f"不支持的导出格式: {fmt}")
    return fn(entries)


# ─────────────────────────────────────────────────────────────────────────
# 软字幕封装（视频/音频 + SRT → 内嵌字幕轨的新文件，不重新编码画面/音轨）
# ─────────────────────────────────────────────────────────────────────────

# 每种容器格式能承载的字幕编解码器不同：MP4/MOV 系只认 mov_text，
# WebM 用 webvtt 更稳妥，其余（如 MKV）走 SRT 文本轨最通用。找不到时
# 兜底退到 "srt"，交给 ffmpeg 自行报错，好过我们在这里瞎猜一个必定
# 失败的编解码器。
_SUBTITLE_CODEC_BY_EXT = {
    ".mp4": "mov_text",
    ".m4v": "mov_text",
    ".mov": "mov_text",
    ".mkv": "srt",
    ".webm": "webvtt",
}


def mux_soft_subtitles(
    src_path: str,
    srt_path: str,
    dst_path: str,
    is_audio_only: bool = False,
) -> str:
    """
    把 SRT 字幕以"软字幕"（独立字幕轨，可在播放器里开关/切换，不烧录进
    画面）方式封装进视频；音频输入则封装成 Matroska Audio（.mka）容器
    ——WAV/MP3/FLAC/AAC 等常见音频容器完全不支持字幕流（ffmpeg 会直接
    报 "muxer does not support any stream of type subtitle" 并失败），
    支持内嵌字幕轨的音频容器本就凤毛麟角，.mka 是最通用的一个，主流
    播放器（VLC、mpv 等）都能识别并显示其中的字幕轨。

    始终用 "-c copy" 拷贝原始视频/音频流，只新增一条字幕流，因此速度
    接近纯文件拷贝、不重新编码，不损失画质/音质。

    参数：
      src_path      : 原始视频/音频文件路径
      srt_path      : 已生成好的 .srt 字幕文件路径
      dst_path      : 输出文件路径。视频输入：后缀决定容器格式，进而
                      决定用哪种字幕编解码器，调用方需自行选一个与
                      src 容器兼容的输出后缀，通常直接沿用原始后缀
                      即可。音频输入：后缀会被忽略，容器强制为
                      Matroska（内部按 .mka 复用 srt 编解码器），调用
                      方应仍传入 .mka 后缀的路径以保持文件名一致，但
                      即便传别的后缀，产出内容也一定是合法的 mka。
      is_audio_only : 源文件是否为纯音频（无视频流）。

    返回：dst_path。失败抛 RuntimeError，附带 ffmpeg 的 stderr 尾部。
    """
    ffmpeg = get_ffmpeg_path()
    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg, "-y",
        "-i", str(src_path),
        "-i", str(srt_path),
        "-map", "0",
        "-map", "1",
        "-c", "copy",
    ]

    if is_audio_only:
        # 强制走 Matroska 复用器：不依赖 dst_path 后缀猜容器（不少音频
        # 输出场景下调用方即便传了 .mka 后缀，也不该假设 ffmpeg 会按
        # 后缀正确选择复用器），显式 "-f matroska" 更可靠。
        cmd += ["-c:s", "srt", "-f", "matroska"]
    else:
        dst_ext = Path(dst_path).suffix.lower()
        subtitle_codec = _SUBTITLE_CODEC_BY_EXT.get(dst_ext, "srt")
        cmd += ["-c:s", subtitle_codec]

    cmd += [
        # 部分播放器（尤其是 mp4/mov）靠这个 metadata 判断字幕轨语言，
        # disposition=default 让播放器默认开启显示，而不是封装进去了
        # 但要用户手动到字幕菜单里选。
        "-metadata:s:s:0", "language=chi",
        "-disposition:s:0", "default",
        str(dst_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 软字幕封装失败: {result.stderr.strip()[-800:]}")
    if not Path(dst_path).exists():
        raise RuntimeError("ffmpeg 执行完成但未生成输出文件")
    return dst_path


# ─────────────────────────────────────────────────────────────────────────
# 硬字幕烧录（纯音频 → 纯色背景视频 + 烧录字幕，兼容所有播放器）
#   与上面的软字幕封装是两条不同路子：纯音频容器（wav/mp3/...）没有
#   画面可言，字幕轨挂在音频占位图上很多播放器根本不渲染（VLC 就是
#   如此），软字幕在"纯音频"场景下体验并不可靠。这里换个思路：造一帧
#   纯色画面撑出一个视频容器，把字幕直接画（烧录）进画面里，牺牲"可
#   关闭字幕"这个软字幕特性，换取"任何播放器打开都能看见"的确定性。
# ─────────────────────────────────────────────────────────────────────────

def _escape_ffmpeg_filter_path(path: str) -> str:
    """
    转义传给 ffmpeg 滤镜参数（如 subtitles=<path>）的文件路径。

    ffmpeg 滤镜参数本身用冒号分隔键值对，Windows 路径的盘符冒号
    （如 "C:\\..."）会被误判成参数分隔符导致解析失败。历史上常见的
    做法是把冒号转义成 "\\:"（同时反斜杠转正斜杠），但不同 ffmpeg
    版本对"引号内是否还需要再转义冒号"处理不一致，实测在部分版本
    上会报 "Error parsing a filter description"。

    更稳妥的办法是彻底避开盘符冒号：调用方应优先通过把子进程的
    cwd 设为字幕文件所在目录、只在滤镜里写不含盘符的文件名来规避
    这个问题（见 burn_subtitles_to_video）。本函数保留作为兜底/其他
    调用点的通用转义，仍按惯用写法处理。
    """
    p = path.replace("\\", "/")
    p = p.replace(":", r"\:")
    return p


_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _parse_srt_timestamp(ts: str) -> float:
    m = _SRT_TIME_RE.search(ts)
    if not m:
        return 0.0
    h, mi, s, ms = m.groups()
    ms = ms.ljust(3, "0")[:3]
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000.0


def _format_ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    total_cs = round(seconds * 100)  # ASS 时间精度到 1/100 秒
    h, rem = divmod(total_cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _parse_srt_cues(srt_content: str) -> List[Tuple[float, float, str]]:
    """
    把 SRT 文本解析成 (start_sec, end_sec, text) 三元组列表，按出现顺序。

    容错：允许缺失序号行；文本可以跨多行；用空行分隔各条目（允许
    \\r\\n / \\n 混用、允许多个连续空行）。
    """
    blocks = re.split(r"\r?\n\r?\n+", srt_content.strip())
    cues: List[Tuple[float, float, str]] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        block_lines = block.splitlines()
        idx = 0
        if idx < len(block_lines) and block_lines[idx].strip().isdigit():
            idx += 1
        if idx >= len(block_lines) or "-->" not in block_lines[idx]:
            continue
        start_str, end_str = [p.strip() for p in block_lines[idx].split("-->")[:2]]
        idx += 1
        text = "\n".join(block_lines[idx:]).strip()
        if not text:
            continue
        start = _parse_srt_timestamp(start_str)
        end = _parse_srt_timestamp(end_str)
        cues.append((start, end, text))
    return cues


def _srt_text_to_ass(text: str) -> str:
    """把 SRT 单条字幕文本转成 ASS Dialogue 的 Text 字段。"""
    # SRT 换行 -> ASS 换行符 \N；花括号会被 ASS 当成样式覆写指令，转义掉
    # 避免字幕正文里偶然出现的 { } 被当成 ASS 标签解析。
    text = text.replace("{", r"\{").replace("}", r"\}")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return r"\N".join(lines)


def _srt_to_ass(srt_content: str, *, font_size: int, margin_v: int) -> str:
    """
    把 SRT 字幕转换成内嵌样式的 ASS 字幕文本。

    【历史说明】这是"用 ffmpeg subtitles 滤镜烧录"方案的一部分。后来
    实测发现用户 Windows 上的 ffmpeg 构建根本没有编译 subtitles 滤镜
    （报 "No such filter: 'subtitles'"，该滤镜依赖 libass，很多精简/
    静态编译的 Windows ffmpeg 会直接砍掉），所以 burn_subtitles_to_video
    已改用完全不依赖任何可选滤镜的"逐帧图片 + concat 拼接"方案（见下方
    _render_subtitle_frame / burn_subtitles_to_video）。本函数保留
    不再被调用，只是为了将来如果确认某个部署环境的 ffmpeg 确实带
    libass、想切回更省资源的滤镜方案时可以直接复用，不必重写。
    """
    # PrimaryColour=&H00FFFFFF（白字）/ OutlineColour=&H00000000（黑边）
    # 是 force_style 惯用写法里的 &HAABBGGRR；ASS 标准样式表里同一个
    # 字段用的也是这个格式，直接照抄即可。
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: None\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,"
        f"&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,20,20,{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    lines_out: List[str] = []
    for start_sec, end_sec, text in _parse_srt_cues(srt_content):
        start = _format_ass_timestamp(start_sec)
        end = _format_ass_timestamp(end_sec)
        ass_text = _srt_text_to_ass(text)
        lines_out.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{ass_text}")
    return header + "\n".join(lines_out) + "\n"


def _hex_bg_to_rgb(bg_color: str) -> Tuple[int, int, int]:
    """把 ffmpeg 风格的颜色写法（如 "0x1a1a2e"）转成 Pillow 用的 RGB 三元组。"""
    s = bg_color.strip()
    if s.startswith(("0x", "0X")):
        s = s[2:]
    elif s.startswith("#"):
        s = s[1:]
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (ValueError, IndexError):
        return (26, 26, 46)  # 解析失败时退回原默认色 0x1a1a2e


_CJK_FONT_CANDIDATES = [
    # 统一只用一款字体渲染全部字幕（不做逐字符/逐语言的字体回退），
    # 优先级按"简体中文字符集覆盖面"排列——微软雅黑（msyh.ttc）是
    # Windows 简体中文系统自带的界面字体，字符集比日语界面字体
    # （Meiryo/Yu Gothic/MS Gothic）广得多：常用简体汉字、以及绝大多数
    # 日语汉字（大部分和简体/繁体汉字同源）都能显示，不会出现缺字变
    # 方块（"tofu"）的问题；代价是个别字形写法会偏"中文手写习惯"而
    # 非日语标准字形，但这只是风格差异，不影响可读性，优先级远低于
    # "字能不能显示出来"。
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    # 找不到简体中文字体时才退回日文界面字体（能显示假名和日语汉字，
    # 但会缺一部分简体中文专用字符，如"乐""缓"等）。
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\yugothm.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
    r"C:\Windows\Fonts\malgun.ttf",
    # Linux（容器/CI 环境常见路径，方便本地测试，生产是 Windows）：
    # Noto Sans CJK 的 "Regular.ttc" 本身是多语言变体合集，字符集覆盖
    # 最广，放在 Linux 候选里第一位。
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
]

_cjk_font_path_cache: Optional[str] = None


def _find_cjk_font() -> str:
    """
    找一个能显示日/中/韩文字的字体文件路径。

    找不到就抛错——找不到能显示 CJK 的字体，硬编码回退到 Arial 只会
    把字全部画成方块（"tofu"），对用户来说跟直接报错让他装字体/换路径
    没有本质区别，不如尽早暴露问题。
    """
    global _cjk_font_path_cache
    if _cjk_font_path_cache and Path(_cjk_font_path_cache).exists():
        return _cjk_font_path_cache
    for candidate in _CJK_FONT_CANDIDATES:
        if Path(candidate).exists():
            _cjk_font_path_cache = candidate
            return candidate
    raise RuntimeError(
        "找不到可用的中日韩字体文件（已尝试 Windows 系统字体常见路径，"
        "如 msyh.ttc / meiryo.ttc / msgothic.ttc 等，均不存在）。"
        "请确认 Windows 已安装东亚语言字体，或联系开发者调整"
        "_CJK_FONT_CANDIDATES 里的候选路径。"
    )


def _render_subtitle_frame(
    text: Optional[str],
    *,
    width: int,
    height: int,
    bg_rgb: Tuple[int, int, int],
    font_path: str,
    font_size: int,
    margin_v: int,
) -> "Image.Image":
    """
    画一帧字幕图：纯色背景 + 底部居中的白字黑边文本（text 为 None/空
    时只画背景，用于两条字幕之间的空档）。
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (width, height), bg_rgb)
    if not text:
        return img
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(font_path, font_size, index=0)
    except OSError:
        font = ImageFont.truetype(font_path, font_size)

    lines = [ln for ln in text.splitlines() if ln.strip()] or [text]
    # 描边宽度按字号比例给（跟原 force_style 里 Outline=2 对应 28pt 字号
    # 的比例大致换算），行距用字号的 1.3 倍。
    stroke_width = max(1, round(font_size * 2 / 28))
    line_spacing = round(font_size * 1.3)
    total_text_h = line_spacing * len(lines)
    y = height - margin_v - total_text_h
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        line_w = bbox[2] - bbox[0]
        x = (width - line_w) / 2 - bbox[0]
        draw.text(
            (x, y), line, font=font, fill=(255, 255, 255),
            stroke_width=stroke_width, stroke_fill=(0, 0, 0),
        )
        y += line_spacing
    return img


def burn_subtitles_to_video(
    audio_path: str,
    srt_path: str,
    dst_path: str,
    duration_sec: float,
    width: int = 1280,
    height: int = 720,
    bg_color: str = "0x1a1a2e",
) -> str:
    """
    把 SRT 字幕烧录（硬字幕，不可关闭）进一段纯色背景画面，与原始音频
    合成一个标准 mp4，供"纯音频 + 字幕"想要在任意播放器直接看到文字"
    的场景使用（是 mux_soft_subtitles 在纯音频输入上的体验短板的补充
    方案，两者并存，不是互相替代）。

    参数：
      audio_path   : 原始音频文件路径（不重新处理其内容，直接编码为
                      AAC；ffmpeg 遇到无法直接 copy 的编码差异时会自动
                      转码，不会报错，只是不再是"无损拷贝"）
      srt_path     : 已生成好的 .srt 字幕文件路径
      dst_path     : 输出 .mp4 路径
      duration_sec : 音频总时长（秒）；纯色背景视频源本身没有天然时长，
                      需要显式告诉 ffmpeg 生成到哪里为止，同时也用
                      "-shortest" 兜底，双重保险防止输出比音频长/短
      width/height : 背景画面分辨率，字幕字号按此换算
      bg_color     : 背景颜色（ffmpeg 颜色写法，如 "0x1a1a2e" 或
                      "black"）

    返回：dst_path。失败抛 RuntimeError，附带 ffmpeg 的 stderr 尾部。

    【实现方式的修复历史】最初用 ffmpeg 的 subtitles 滤镜（-vf
    "subtitles=..."）实现，中途先后修了盘符冒号转义、force_style 逗号
    转义两个问题，但最终发现用户 Windows 上的 ffmpeg 构建根本没有编译
    subtitles 滤镜（ffmpeg 报 "No such filter: 'subtitles'"——该滤镜
    依赖 libass，是可选编译项，不少精简/静态构建会砍掉），前面两次
    "修好了语法但还是报错"其实都是这同一个根因在不同参数形态下走了
    ffmpeg 内部不同的报错分支，并不是真的因为语法本身有问题。

    现在改用完全不依赖任何可选滤镜的方案：字幕文字用 Pillow 在 Python
    里直接画成一张张静态图片（纯色背景 + 白字黑边），再用 ffmpeg 把
    每张图片精确重复到该显示的时长、首尾拼接成视频、叠上音轨（具体
    拼接写法见下方"重要"注释——踩过 concat 拼接器给图片配 duration
    指令的一个长期未修复的 ffmpeg 上游 bug，最终改用了 concat 滤镜 +
    每路输入各自 -loop/-t 定长这种写法）。用到的滤镜/解复用功能全部
    是任何 ffmpeg 构建都必带的核心功能，不存在"这个构建有没有编译
    某个可选滤镜"的不确定性，从根源上避免再次踩中同一类坑。
    """
    ffmpeg = get_ffmpeg_path()
    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)

    srt_resolved = Path(srt_path).resolve()
    srt_content = srt_resolved.read_text(encoding="utf-8-sig", errors="replace")
    cues = _parse_srt_cues(srt_content)

    font_size = max(16, round(28 * height / 720))
    margin_v = max(20, round(40 * height / 720))
    bg_rgb = _hex_bg_to_rgb(bg_color)
    font_path = _find_cjk_font()

    # 按时间顺序把 [0, duration_sec] 切成连续、不重叠的片段：字幕条目
    # 本身各占一段，条目之间/首尾的空档各补一段"只有背景、没有文字"的
    # 片段，保证任意时刻都有画面覆盖。
    segments: List[Tuple[float, float, Optional[str]]] = []
    cursor = 0.0
    for start, end, text in cues:
        start = max(start, cursor)
        end = max(end, start)
        if end <= start:
            continue  # 时间戳异常（如与前一条完全重叠）时跳过，不产生零长度片段
        if start > cursor:
            segments.append((cursor, start, None))
        segments.append((start, end, text))
        cursor = end
    if cursor < duration_sec:
        segments.append((cursor, duration_sec, None))
    if not segments:
        segments = [(0.0, max(duration_sec, 0.1), None)]

    # 相同文本（含"空背景"这种 text=None）只画一次图，多个片段复用同一
    # 张图片文件，减少 Pillow 渲染次数和磁盘占用。
    work_dir = srt_resolved.parent
    frame_paths: Dict[Optional[str], Path] = {}
    for _, _, text in segments:
        if text not in frame_paths:
            img = _render_subtitle_frame(
                text, width=width, height=height, bg_rgb=bg_rgb,
                font_path=font_path, font_size=font_size, margin_v=margin_v,
            )
            frame_name = f"{srt_resolved.stem}_frame{len(frame_paths):04d}.png"
            frame_path = work_dir / frame_name
            img.save(frame_path)
            frame_paths[text] = frame_path

    # 【重要】这里特意不用 "-f concat" 拼接器给图片配 duration 指令——
    # 这是 ffmpeg 一个长期存在、未修复的已知 bug（上游 Trac #6128，
    # 2017 年提出至今仍未解决）：拼接器给静态图片指定 duration 时，
    # 算出来的总时长经常是错的（实测偏差能到几十%，不是简单的取整
    # 误差），跟具体平台/构建无关，纯粹是这个功能本身不可靠。
    #
    # 改用更精确可靠的写法：每个片段各自作为一路独立输入，用
    # "-loop 1 -t <该片段时长> -i 图片" 让 ffmpeg 在解复用层面就把
    # 每张图片精确重复/裁剪到需要的时长，再用 concat 滤镜（不是
    # 拼接器，是 libavfilter 里的 vf_concat，零外部依赖、任何构建都
    # 一定带）把这些已经定长的视频流首尾接起来。实测这种写法总时长
    # 完全精确，不存在上面那个 bug。
    input_args: List[str] = []
    concat_filter_parts: List[str] = []
    for i, (seg_start, seg_end, text) in enumerate(segments):
        seg_duration = max(0.02, seg_end - seg_start)
        input_args += [
            "-loop", "1", "-t", f"{seg_duration:.3f}",
            "-i", frame_paths[text].name,
        ]
        concat_filter_parts.append(f"[{i}:v]fps=24,format=yuv420p[v{i}]")
    concat_inputs = "".join(f"[v{i}]" for i in range(len(segments)))
    filter_complex = ";".join(concat_filter_parts)
    filter_complex += f";{concat_inputs}concat=n={len(segments)}:v=1:a=0[vout]"

    audio_input_index = len(segments)
    cmd = [
        ffmpeg, "-y",
        *input_args,
        "-i", str(Path(audio_path).resolve()),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", f"{audio_input_index}:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-t", str(max(0.1, duration_sec)),
        "-shortest",
        str(Path(dst_path).resolve()),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=1800, cwd=str(work_dir),
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 字幕烧录失败: {result.stderr.strip()[-800:]}")
    if not Path(dst_path).exists():
        raise RuntimeError("ffmpeg 执行完成但未生成输出文件")

    # 清理中间产物（逐帧 PNG），只留最终 mp4。
    for p in frame_paths.values():
        p.unlink(missing_ok=True)

    return dst_path
