# subtitle_import.py
# -*- coding: utf-8 -*-
"""
字幕导入（SRT/LRC → 逐句强制对齐）处理模块。

与 subtitle_processor.py（音频 → 字幕，ASR 方向）正好相反：这里的输入是
用户已经有的一份字幕文件（SRT 或 LRC）+ 对应的完整音频，目的是把字幕的
时间轴当作"切分点 + 参考文本"的来源，按每一条字幕独立切出一小段音频，
固定交给 Qwen3-ForcedAligner 做逐句强制对齐，而不是对整段音频一次性做
长音频对齐——短句对齐通常比长音频对齐更准，且不受 Qwen3 内部窗口切分
的退化时间戳问题影响（参见 alt_aligners.py 顶部对"退化区间"的说明）。

核心流程：
  1) 解析字幕文件，得到 [(start_sec, end_sec, text), ...]（见
     parse_srt / parse_lrc）。
       - SRT 自带起止时间。
       - LRC 只有起始时间：每一条的结束时间用下一条的起始时间推断；
         最后一条没有下一条可推断，使用整个音频文件的时长作为结束时间。
  2) 相邻两条字幕之间如果有空白间隙（说话人停顿、背景音等），这段间隙
     音频不会被丢弃——作为独立的"静音段"保留在两条字幕之间，音频总长
     与原始音频完全一致；这样切分后按顺序拼接回去（或原样交给下游按
     顺序在时间轴上背靠背排列）不会丢失任何一帧原始音频。
  3) 逐条字幕对应的音频切片单独调用 Qwen3-FA 对齐（参考 tts_processor.py
     的 align_segments 实现风格），失败时退化为均匀分配时间戳兜底，
     不会因为个别句子对齐失败中断整个批次。
  4) 提供两种消费方式：
       - build_dialogue_boxes()：面向"对话文本框批量处理"（DialogueBatch.vue），
         每条字幕切一个独立 WAV 文件，返回 (text, wav_path) 列表，调用方
         直接组装成 process_dialogue_batch 需要的 boxes，静音段插入为
         附近文本为空的"占位框"由调用方决定是否保留（batch 场景通常
         直接丢弃静音段——用户只关心有台词的那些框，静音会在最终工程
         文件里因为按真实音频时长背靠背排列而自然保留，见下方
         merge_gaps_into_neighbors 参数）。
       - align_subtitle_audio()：面向"单文件处理"（MFAProcessor.vue），
         对整段音频做一次性处理：逐条字幕切片 → 对齐 → 按偏移拼接回
         一份完整 WAV + 一份完整 LAB（静音段落原样拼接，不对齐，直接
         输出为 LAB 里的 SIL），效果上等价于对整段音频做了一次"由字幕
         导航"的强制对齐。
"""
from __future__ import annotations

import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# 字幕解析：SRT / LRC → [(start_sec, end_sec, text), ...]
# ─────────────────────────────────────────────────────────────────────────

class SubtitleCue:
    __slots__ = ("start", "end", "text")

    def __init__(self, start: float, end: float, text: str):
        self.start = start
        self.end = end
        self.text = text

    def __repr__(self) -> str:
        return f"SubtitleCue({self.start:.3f}, {self.end:.3f}, {self.text!r})"


_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
)


def _srt_ts_to_sec(h: str, m: str, s: str, ms: str) -> float:
    # 毫秒字段允许 1~3 位（少数不规范 SRT 只写 1~2 位），统一按左对齐补零
    # 到 3 位解读（"5" -> "500ms" 而不是 "5ms"），与大多数播放器的解读一致。
    ms_padded = (ms + "000")[:3]
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms_padded) / 1000.0


def parse_srt(content: str) -> List[SubtitleCue]:
    """
    解析 SRT 字幕文本，返回按时间排序的 SubtitleCue 列表。

    容错处理：
      - 允许缺失/非数字的序号行（部分工具导出的 SRT 省略序号）。
      - 允许毫秒用逗号或句点分隔（标准是逗号，但常见到句点变体）。
      - 一条字幕的文本可能跨多行，直到下一个时间轴行或空行为止。
      - 结束时间早于或等于开始时间的畸形条目会被跳过（避免产出负时长
        或零时长切片，下游切音频会失败）。
    """
    cues: List[SubtitleCue] = []
    # 按空行分块，每块内部找时间轴行，其余非空行拼成文本。
    blocks = re.split(r"\r?\n\s*\r?\n", content.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        time_line_idx = None
        m = None
        for i, ln in enumerate(lines):
            m = _SRT_TIME_RE.search(ln)
            if m:
                time_line_idx = i
                break
        if m is None or time_line_idx is None:
            continue
        start = _srt_ts_to_sec(*m.groups()[0:4])
        end = _srt_ts_to_sec(*m.groups()[4:8])
        text_lines = lines[time_line_idx + 1:]
        text = "\n".join(text_lines).strip()
        if end <= start or not text:
            continue
        cues.append(SubtitleCue(start, end, text))

    cues.sort(key=lambda c: c.start)
    return cues


_LRC_TIME_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_LRC_META_TAG_RE = re.compile(r"^\[[a-zA-Z]+:.*\]$")


def parse_lrc(content: str, audio_duration_sec: Optional[float] = None,
              fallback_last_duration_sec: float = 5.0) -> List[SubtitleCue]:
    """
    解析 LRC 歌词文本，返回按时间排序的 SubtitleCue 列表。

    LRC 每行只有起始时间标签（可能同一行有多个时间标签对应同一句歌词，
    这里逐个标签都展开成独立条目，随后统一按时间排序），没有结束时间——
    每一条的结束时间用"下一条的起始时间"推断（说话人在下一句开口前，
    这段时间都算作当前句，包含其后的自然停顿）；最后一条没有下一条可
    推断，使用 audio_duration_sec（整个音频文件的时长）作为结束时间；
    若调用方未提供音频时长（极少数场景，如只想单纯解析文本不切音频），
    才回退使用 fallback_last_duration_sec 作为固定兜底时长。

    形如 [ar:...]/[ti:...]/[al:...] 等元信息标签行会被忽略；一行内既有
    元信息标签又有时间标签的混合写法不常见，这里只要一行内出现了任意
    时间标签就按歌词行处理，避免过度设计。
    """
    raw_entries: List[Tuple[float, str]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if _LRC_META_TAG_RE.match(line):
            continue
        tags = list(_LRC_TIME_TAG_RE.finditer(line))
        if not tags:
            continue
        text = _LRC_TIME_TAG_RE.sub("", line).strip()
        if not text:
            continue
        for m in tags:
            minutes = int(m.group(1))
            seconds = int(m.group(2))
            frac_raw = m.group(3)
            if frac_raw is None:
                frac = 0.0
            elif len(frac_raw) == 1:
                frac = int(frac_raw) / 10.0
            elif len(frac_raw) == 2:
                frac = int(frac_raw) / 100.0
            else:
                frac = int(frac_raw) / 1000.0
            start = minutes * 60 + seconds + frac
            raw_entries.append((start, text))

    raw_entries.sort(key=lambda t: t[0])

    cues: List[SubtitleCue] = []
    n = len(raw_entries)
    for i, (start, text) in enumerate(raw_entries):
        if i + 1 < n:
            end = raw_entries[i + 1][0]
        elif audio_duration_sec is not None:
            end = audio_duration_sec
        else:
            end = start + fallback_last_duration_sec
        if end <= start:
            continue
        cues.append(SubtitleCue(start, end, text))

    return cues


def detect_subtitle_format(filename: str, content: str) -> str:
    """按扩展名优先、内容兜底的方式判断字幕格式，返回 'srt' / 'lrc'。"""
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext in ("srt", "lrc"):
        return ext
    # 扩展名不认识（如 .txt）：内容里出现 "-->" 视为 SRT，出现
    # "[mm:ss" 形式的时间标签视为 LRC，都不像则默认按 SRT 尝试解析。
    if "-->" in content:
        return "srt"
    if _LRC_TIME_TAG_RE.search(content):
        return "lrc"
    return "srt"


def parse_subtitle_file(filename: str, content: str,
                         audio_duration_sec: Optional[float] = None) -> Tuple[str, List[SubtitleCue]]:
    """
    自动判断格式并解析，返回 (format, cues)。

    content 需要已经按文本读取（调用方负责处理编码探测，常见的 SRT/LRC
    多为 UTF-8 或 UTF-8 BOM，个别老文件是 GBK——见 app.py 路由层的
    _read_subtitle_text 编码兜底逻辑）。
    """
    fmt = detect_subtitle_format(filename, content)
    if fmt == "lrc":
        cues = parse_lrc(content, audio_duration_sec=audio_duration_sec)
    else:
        cues = parse_srt(content)
    return fmt, cues


# ─────────────────────────────────────────────────────────────────────────
# 间隙处理：把字幕 cue 列表 + 音频总时长，展开成"文本段 + 静音段"的完整
# 时间轴分段（覆盖 [0, audio_duration] 的每一秒，不丢失任何原始音频）。
# ─────────────────────────────────────────────────────────────────────────

class TimelineSegment:
    __slots__ = ("start", "end", "text", "is_gap")

    def __init__(self, start: float, end: float, text: str, is_gap: bool):
        self.start = start
        self.end = end
        self.text = text
        self.is_gap = is_gap

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


MIN_GAP_SEC = 0.03  # 短于这个时长的间隙视为浮点误差/字幕时间轴的正常缝隙，不单独生成静音段


def build_timeline(cues: List[SubtitleCue], audio_duration_sec: float,
                    min_gap_sec: float = MIN_GAP_SEC) -> List[TimelineSegment]:
    """
    把字幕 cue 列表铺满整条时间轴 [0, audio_duration_sec]：
      - cue 覆盖到的区间 → 文本段（is_gap=False）
      - cue 之间 / 开头 / 结尾的空白 → 静音段（is_gap=True）

    保证返回的分段按时间顺序首尾相接、完整覆盖 [0, audio_duration_sec]，
    不重叠、不留空洞——这是"音频总长与原音频完全一致"的关键。

    相邻 cue 时间轴重叠（字幕文件本身有问题，起止时间交叉）时，后一条
    的起始时间会被夹紧到前一条的结束时间，避免产出负时长切片；重叠部分
    不做更复杂的仲裁，尊重字幕文件里条目出现的先后顺序。
    """
    segments: List[TimelineSegment] = []
    cursor = 0.0

    for cue in cues:
        start = max(cue.start, cursor)
        end = min(cue.end, audio_duration_sec)
        if end <= start:
            continue
        if start - cursor >= min_gap_sec:
            segments.append(TimelineSegment(cursor, start, "", is_gap=True))
        segments.append(TimelineSegment(start, end, cue.text, is_gap=False))
        cursor = end

    if audio_duration_sec - cursor >= min_gap_sec:
        segments.append(TimelineSegment(cursor, audio_duration_sec, "", is_gap=True))
    elif segments:
        # 末尾残留的极小间隙（< min_gap_sec）直接并入最后一段，保证总时长
        # 精确覆盖到 audio_duration_sec，不留下未被任何分段覆盖的尾巴。
        segments[-1].end = audio_duration_sec

    return segments


# ─────────────────────────────────────────────────────────────────────────
# 音频切片（复用 subtitle_processor 的 ffmpeg 路径探测与切片实现）
# ─────────────────────────────────────────────────────────────────────────

def _slice_wav(wav_path: str, start_sec: float, end_sec: float, out_path: str) -> str:
    from subtitle_processor import _slice_wav as _impl
    return _impl(wav_path, start_sec, end_sec, out_path)


def _probe_duration_sec(wav_path: str) -> float:
    from subtitle_processor import probe_duration_sec
    return probe_duration_sec(wav_path)


# ─────────────────────────────────────────────────────────────────────────
# 逐句 Qwen3-FA 强制对齐 + 拼接（对话文本框批量处理场景：每条字幕单独一个 WAV）
# ─────────────────────────────────────────────────────────────────────────

def build_dialogue_boxes(
    wav_path: str,
    cues: List[SubtitleCue],
    work_dir: str,
    stem_prefix: str = "sub",
    audio_duration_sec: Optional[float] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Dict:
    """
    按字幕时间轴把 wav_path 切成若干独立的小 WAV 文件（每条字幕一个），
    供"对话文本框批量处理"（DialogueBatch.vue）直接当作已上传的音频+
    文本使用——每个切片本身仍会在后续 process_dialogue_batch 里各自跑
    一次 Qwen3-FA 逐句强制对齐（不在这一步预先对齐），这样与用户手动
    逐框上传音频、由现有批量处理流程统一对齐的行为完全一致，也天然支持
    每个框独立设置"单独设置"（对齐后端/语言等）。

    静音间隙不生成对话框（DialogueBatch 的每个框都需要一段有台词的音频
    才有意义），但会被写入 gap_segments 返回给调用方参考（例如前端可以
    提示"共 N 处停顿被跳过，总计 X 秒"）；由于最终工程文件是按每个对话框
    的真实音频时长背靠背拼接在同一时间轴上，跳过静音段本质上是"掐掉了
    停顿"而不是"错位"——这是从字幕批量生成对话框这个场景下的合理默认
    行为（用户通常想要的是"去掉空白后逐句配音/配唱"，而不是完整保留
    原片间隙）。

    Returns
    -------
    Dict:
      成功: {
        "success": True,
        "boxes": [{"text": str, "wav_path": str, "start": float, "end": float}, ...],
        "gap_segments": [{"start": float, "end": float}, ...],
        "cue_count": int,
      }
      失败: {"success": False, "error": str}
    """
    if not cues:
        return {"success": False, "error": "字幕文件未解析出任何有效条目"}

    duration = audio_duration_sec if audio_duration_sec is not None else _probe_duration_sec(wav_path)
    timeline = build_timeline(cues, duration)

    text_segments = [s for s in timeline if not s.is_gap]
    gap_segments = [s for s in timeline if s.is_gap]

    if not text_segments:
        return {"success": False, "error": "字幕时间轴与音频时长不匹配，未能切出任何有效片段"}

    work_dir_path = Path(work_dir)
    slices_dir = work_dir_path / f"_subtitle_slices_{stem_prefix}_{uuid.uuid4().hex[:8]}"
    slices_dir.mkdir(parents=True, exist_ok=True)

    boxes: List[Dict] = []
    total = len(text_segments)
    try:
        for i, seg in enumerate(text_segments):
            out_wav = slices_dir / f"{stem_prefix}_{i:04d}.wav"
            _slice_wav(wav_path, seg.start, seg.end, str(out_wav))
            boxes.append({
                "text": seg.text,
                "wav_path": str(out_wav),
                "start": seg.start,
                "end": seg.end,
            })
            if progress_cb:
                progress_cb(i + 1, total)
    except Exception as e:
        shutil.rmtree(str(slices_dir), ignore_errors=True)
        logger.error(f"[字幕导入] 音频切片失败: {e}", exc_info=True)
        return {"success": False, "error": f"音频切片失败: {e}"}

    return {
        "success": True,
        "boxes": boxes,
        "slices_dir": str(slices_dir),
        "gap_segments": [{"start": s.start, "end": s.end} for s in gap_segments],
        "cue_count": len(text_segments),
    }


# ─────────────────────────────────────────────────────────────────────────
# 逐句 Qwen3-FA 强制对齐 + 拼接（单文件处理场景：产出一份完整 WAV + LAB）
# ─────────────────────────────────────────────────────────────────────────

def align_subtitle_audio(
    wav_path: str,
    cues: List[SubtitleCue],
    language: str,
    aligner_device: Optional[str] = None,
    english_word_align: bool = False,
    align_pitch_shift_semitones: float = 0.0,
    audio_duration_sec: Optional[float] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Dict:
    """
    单文件"字幕跟读"主流程：整段音频按字幕时间轴切分 → 每条字幕独立音频
    片段固定交给 Qwen3-ForcedAligner 做逐句强制对齐 → 每句的 LAB 时间戳
    按其在原始音频里的真实起始时间整体平移 → 与静音间隙段（原样保留为
    SIL，不参与对齐）一起拼成覆盖整段音频的最终 LAB。

    输出的 LAB 时间戳直接对应传入的原始 wav_path（不需要拼接新音频、
    不改变音频本身），可以和 project-only / full 流程一样直接使用。

    实现风格与 tts_processor.align_segments 一致（逐句对齐 + 偏移平移 +
    失败兜底），区别在于这里的"分句音频"来自对原始音频切片，而不是
    TTS 逐句合成。

    Returns
    -------
    Dict:
      成功: {
        "success": True, "lab_content": str, "cue_count": int,
        "warnings": [str, ...], "audio_duration": int,  # 100ns
      }
      失败: {"success": False, "error": str}
    """
    import os
    from alt_aligners import get_aligner, _fill_silences_lab
    from tts_processor import (
        _parse_lab_lines, _shift_entries, _entries_to_lab_text,
        _uniform_fallback_entries, _get_wav_duration_100ns,
        _make_alignment_pitch_shifted_copy,
    )

    if not cues:
        return {"success": False, "error": "字幕文件未解析出任何有效条目"}

    duration = audio_duration_sec if audio_duration_sec is not None else _probe_duration_sec(wav_path)
    audio_duration_100ns = _get_wav_duration_100ns(wav_path)
    timeline = build_timeline(cues, duration)

    text_segments = [s for s in timeline if not s.is_gap]
    if not text_segments:
        return {"success": False, "error": "字幕时间轴与音频时长不匹配，未能切出任何有效片段"}

    try:
        aligner = get_aligner("qwen3_aligner", device=(aligner_device or "auto"))
    except Exception as e:
        return {"success": False, "error": f"Qwen3-ForcedAligner 初始化失败: {e}"}

    work_dir_path = Path(wav_path).parent
    tmp_dir = work_dir_path / f"_subtitle_align_{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    lab_entries: List[Tuple[int, int, str]] = []
    warnings: List[str] = []
    total = len(text_segments)

    try:
        for i, seg in enumerate(text_segments):
            seg_wav = tmp_dir / f"seg_{i:04d}.wav"
            _slice_wav(wav_path, seg.start, seg.end, str(seg_wav))
            seg_duration_100ns = _get_wav_duration_100ns(str(seg_wav))
            offset_100ns = int(round(seg.start * 10_000_000))

            align_target = str(seg_wav)
            shifted_path: Optional[str] = None
            if align_pitch_shift_semitones:
                try:
                    shifted_path = _make_alignment_pitch_shifted_copy(
                        str(seg_wav), align_pitch_shift_semitones
                    )
                    align_target = shifted_path
                except Exception as e:
                    logger.warning(f"[字幕导入] 第 {i + 1}/{total} 句对齐辅助移调失败，回退为原始音频对齐: {e}")

            try:
                align_result = aligner.align(align_target, seg.text, language,
                                              english_word_align=english_word_align)
            except Exception as e:
                align_result = {"success": False, "error": str(e)}
            finally:
                if shifted_path:
                    try:
                        os.remove(shifted_path)
                    except OSError:
                        pass

            if align_result.get("success"):
                local_entries = _parse_lab_lines(align_result.get("lab_content", ""))
            else:
                logger.warning(
                    f"[字幕导入] 第 {i + 1}/{total} 句 Qwen3-FA 对齐失败，"
                    f"使用均匀分配兜底：{align_result.get('error')}"
                )
                warnings.append(f"第 {i + 1} 句对齐失败，已使用均匀时间戳兜底：{align_result.get('error')}")
                local_entries = _uniform_fallback_entries(seg.text, seg_duration_100ns)

            lab_entries.extend(_shift_entries(local_entries, offset_100ns))

            if progress_cb:
                progress_cb(i + 1, total)
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)

    lab_entries.sort(key=lambda t: t[0])

    # 【重要】_fill_silences_lab 只负责补全"条目之间"以及"音频开头"（首条目
    # 起始时间 > 阈值时）的间隙，并不知道整段音频的真实总时长，因此不会
    # 补全末尾的静音——如果最后一条字幕的结束时间早于音频总长（例如
    # 末尾还有一段没有字幕覆盖的静音/背景音），需要在这里显式补上最后
    # 一段 SIL，覆盖到 audio_duration_100ns，否则生成的 LAB 会比原始
    # 音频短一截，与"音频总长与原音频完全一致"的设计要求不符。
    if lab_entries:
        last_end = lab_entries[-1][1]
        if audio_duration_100ns - last_end > 500_000:  # > 50ms，与 _fill_silences_lab 的阈值一致
            lab_entries.append((last_end, audio_duration_100ns, "SIL"))

    raw_lab_text = _entries_to_lab_text(lab_entries)
    # _fill_silences_lab 会自动在条目之间 / 开头 / 结尾插入 SIL 补齐间隙，
    # 效果上等价于把静音段（is_gap 分段）原样保留进最终 LAB，不需要
    # 单独处理 gap 分段——只要每个文本段的时间戳都锚定在正确的绝对位置。
    final_lab_text = _fill_silences_lab(raw_lab_text) if raw_lab_text else ""

    return {
        "success": True,
        "lab_content": final_lab_text,
        "cue_count": total,
        "warnings": warnings,
        "audio_duration": audio_duration_100ns,
    }
