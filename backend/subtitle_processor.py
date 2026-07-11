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
  3) 逐块调用 qwen3_server.py 的 /asr 接口做识别（return_time_stamps=True，
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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
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
    把 qwen3_server 返回的 (text, time_stamps) 归一化为
    [(char_or_token, start_sec, end_sec), ...]，坐标已经加上 block_start
    偏移（qwen3_server 返回的是相对该次请求音频片段起点的时间）。

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
    两条。"遇到逗号就切成下一条字幕"的场景请用 _split_at_every_clause_punct，
    不经过这个函数。

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


def _split_at_every_clause_punct(text: str) -> List[str]:
    """
    把 text 在每一个逗号/顿号/句末标点处都切开（不只是找离中点最近的
    那一个），标点本身归入它前面的那一段。用于"遇到逗号就切成下一条
    字幕"这种场景——例如"大家好，你好。" 会被切成 ["大家好，", "你好。"]。

    连续标点（如"……""，。"）不会产生空段；文本结尾的标点不会切出一个
    空的尾段。
    """
    if not text:
        return []
    cut_punct = _SENTENCE_PUNCT + _CLAUSE_PUNCT
    pieces: List[str] = []
    start = 0
    n = len(text)
    for i, ch in enumerate(text):
        if ch in cut_punct:
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
) -> List[Tuple[str, float, float]]:
    """
    把一条字幕拆成多条，每条形如 (text, start_sec, end_sec)。
    char_times 与 text 等长，逐字符对应时间。

    allow_comma_split=True 时，行为不再是"只有超过 max_chars 才尝试在
    标点处切"——而是无条件在每一个逗号/顿号/句末标点处都切开一条新
    字幕（"大家好，你好。" → "大家好，" / "你好。"），标点和长度无关，
    这是用户主动要的"遇到逗号就换一条字幕"效果。切开之后，如果某一段
    仍然超过 max_chars（比如两个逗号之间的内容本身就很长），再对这一段
    递归地按字数在中点附近硬切，保证单条字幕不会失控地长。

    allow_comma_split=False（默认）时保留原来的行为：只有整段超过
    max_chars 才二次拆分，只在句末标点（。！？；等）处切，找不到就按
    字数硬切；逗号完全不参与。
    """
    if not text or not char_times:
        return []

    if allow_comma_split:
        pieces = _split_at_every_clause_punct(text)
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
# 主流程：调用 qwen3_server 逐块识别 → 组装字幕条目
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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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


def transcribe_to_subtitles(
    wav_path: str,
    language: str = "auto",
    endpoint: str = "http://127.0.0.1:5001/asr",
    device: str = "auto",
    max_chars: int = MAX_SUBTITLE_CHARS,
    tmp_dir: Optional[str] = None,
    progress_cb=None,
    allow_comma_split: bool = False,
    remove_punctuation: bool = False,
    close_vad_gaps: bool = False,
    vad_gap_threshold_sec: float = 0.6,
) -> List[SubtitleEntry]:
    """
    对完整 WAV 做 VAD 切句 → 逐块 ASR 识别 → 长句二次拆分，返回按时间
    排序的字幕条目列表。

    progress_cb(done, total) 可选，用于上报进度给调用方（Flask job）。
    allow_comma_split：是否把逗号/顿号也当作切分点（默认 False，只在
        句末标点处切）。开启后不是"超过 max_chars 才尝试"，而是无条件
        遇到逗号/顿号/句末标点就切成下一条字幕——例如"大家好，你好。"
        会变成两条："大家好，" 和 "你好。"。切开后若某一段本身仍然超过
        max_chars，再对这一段按字数在中点附近继续硬切。切分用的是这一
        段音频块内逐字 ASR 时间戳，精确到字。
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
    """
    import requests

    segments = vad_split_segments(wav_path)
    if not segments:
        return []

    asr_lang = resolve_qwen3_language(language)
    session = requests.Session()
    tmp_root = Path(tmp_dir) if tmp_dir else Path(wav_path).parent / f"_subtitle_chunks_{uuid.uuid4().hex[:8]}"
    tmp_root.mkdir(parents=True, exist_ok=True)

    entries: List[SubtitleEntry] = []
    total = len(segments)

    try:
        for i, (seg_start, seg_end) in enumerate(segments):
            chunk_path = tmp_root / f"chunk_{i:05d}.wav"
            _slice_wav(wav_path, seg_start, seg_end, str(chunk_path))

            payload = {
                "audio": str(chunk_path.resolve()),
                "language": asr_lang,
                "context": "",
                "device": device,
            }
            resp = session.post(endpoint, json=payload, timeout=600)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                logger.warning("字幕分块识别失败（第 %d 块）：%s", i, data.get("error"))
                if progress_cb:
                    progress_cb(i + 1, total)
                continue

            raw_segments = data.get("segments") or []
            block_text = "".join((s.get("text") or "") for s in raw_segments).strip()
            if not block_text:
                if progress_cb:
                    progress_cb(i + 1, total)
                continue

            # 合并所有子 segment 的时间戳（qwen3_server 通常单块只返回一个
            # segment，但保留多 segment 的兼容处理）。
            merged_ts: List[List[Optional[float]]] = []
            for s in raw_segments:
                merged_ts.extend(s.get("time_stamps") or [])

            char_times = _char_time_from_segments(block_text, merged_ts, seg_start, seg_end)
            if not char_times:
                char_times = [(block_text, seg_start, seg_end)]  # 极端兜底

            split_entries = _split_long_entry(block_text, char_times, max_chars, allow_comma_split) \
                if len(char_times) == len(block_text) else [(block_text, seg_start, seg_end)]

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


EXPORTERS = {
    "srt": export_srt,
    "lrc": export_lrc,
    "txt": export_txt,
}


def export_subtitles(entries: List[Dict[str, Any]], fmt: str) -> str:
    fn = EXPORTERS.get(fmt)
    if not fn:
        raise ValueError(f"不支持的导出格式: {fmt}")
    return fn(entries)
