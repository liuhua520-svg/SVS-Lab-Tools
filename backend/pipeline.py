# -*- coding: utf-8 -*-
"""
完整处理流程管道（v2.2 — 多后端对齐支持）
整合 MFA / Qwen3-ASR / Qwen3-ForcedAligner / NeMo Forced Aligner + 音高处理 + 工程文件生成

新增参数: aligner_backend
  "mfa"           — Montreal Forced Aligner（默认）
  "qwen3_asr"     — Qwen3-ASR-1.7B (自动语音识别，文本可选)
  "qwen3_aligner" — Qwen3-ForcedAligner-0.6B (强制对齐，需要参考文本)
  "nemo_aligner"  — NeMo Forced Aligner (NVIDIA CTC 强制对齐，需要参考文本)
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional

from mfa_processor import MFAProcessor
from tsubaki_processor import TsubakiProcessor, AudioProcessingConfig

logger = logging.getLogger(__name__)


class _LocalFileAdapter:
    """
    把磁盘上已存在的文件包装成 audio_file 风格的对象（.filename / .save() / .seek()），
    兼容 MFAProcessor.process() / alt_aligners 各 aligner.align() 内部对
    Flask FileStorage 接口的依赖。

    对话文本框批量处理功能（process_dialogue_batch）中，每个对话框的音频
    在路由层已经落盘，这里只需要包一层适配器复用现有的 _run_alignment()，
    不需要重新实现一遍对齐调度逻辑。
    """
    def __init__(self, local_path: str):
        self.path = os.path.abspath(local_path)
        self.filename = os.path.basename(local_path)

    def save(self, dst: str) -> None:
        if os.path.abspath(dst) != self.path:
            shutil.copy(self.path, dst)

    def seek(self, *args, **kwargs) -> None:
        pass


# ═════════════════════════════════════════════════════════════════════════════
# 数字 → 各语种读法文字 转换
#   把参考文本里的阿拉伯数字 0-9 按当前选择的语种转换成对应的读法文字。
#   逐字转换（一三一四 → 一三一四），不做"完整数值"解析（不会把 "1234"
#   读成"一千二百三十四"），与歌词/对齐场景里数字常被逐字唱出的习惯一致
#   （电话号码、年份、编号等通常也是逐字读出）。
#
#   挂在 _run_alignment() 顶部、四个后端分支之前调用一次，MFA /
#   WhisperX / Qwen3-ASR / Qwen3-FA 四个后端因此都能复用同一份转换结果，
#   不需要在各自的对齐逻辑里各自实现一遍。
# ═════════════════════════════════════════════════════════════════════════════

_DIGIT_WORDS: Dict[str, list] = {
    "zh":  ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"],
    "yue": ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"],
    "en":  ["zero", "one", "two", "three", "four",
            "five", "six", "seven", "eight", "nine"],
    "ja":  ["ぜろ", "いち", "に", "さん", "よん",
            "ご", "ろく", "なな", "はち", "きゅう"],
    "ko":  ["영", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구"],
}

# 中/粤文字本身书写不依赖空格，逐字直接拼接（如 "1234" → "一二三四"）。
# 英/日/韩每个数字的读法之间用空格隔开：
#   - 英语词级对齐依赖空格做分词，连写会变成一个词典/G2P 都查不到的
#     生造词（"onetwothree"），必须保留分隔。
#   - 日语/韩语虽然书写习惯上不强制空格，但连续多个数字逐字读出时
#     （如电话号码、编号）保留分隔更贴近真实唱法，也避免相邻数字的
#     读法互相粘连产生歧义（如 いち+に 连写成"いちに"）。
_DIGIT_WORD_SEPARATOR: Dict[str, str] = {
    "zh": "", "yue": "", "en": " ", "ja": " ", "ko": " ",
}

_DIGIT_RUN_RE = re.compile(r"\d+")


def _convert_digits_to_words(text: str, language: str) -> str:
    """
    把 text 里所有阿拉伯数字 0-9 按 language 对应的语种转换成读法文字。

    转换规则（与产品需求给出的样例完全一致）：
      中文/粤语 1234567890 → 一二三四五六七八九零（无分隔符）
      英语      1234567890 → one two three four five six seven eight nine zero
      日语      1234567890 → いち に さん よん ご ろく なな はち きゅう ぜろ
      韩语      1234567890 → 일 이 삼 사 오 육 칠 팔 구 영

    text 中非数字部分原样保留；language 经 _normalize_lang() 规整后若不在
    上表中（暂不支持该语种的数字转换），原样返回 text，不做任何改动。
    text 中本就不含数字时直接返回原文本，不做无意义的字符串重建。
    """
    if not text or not any(ch.isdigit() for ch in text):
        return text

    # 延迟导入：避免给 MFA-only 的主路径增加 alt_aligners.py 的导入开销，
    # 与 _run_alignment() 里对替代后端分支的现有延迟导入风格保持一致。
    from alt_aligners import _normalize_lang

    int_lang = _normalize_lang(language)
    words = _DIGIT_WORDS.get(int_lang)
    if not words:
        logger.debug(f"数字转换：语种 '{language}'（规整为 '{int_lang}'）暂不支持，文本原样保留")
        return text

    sep = _DIGIT_WORD_SEPARATOR.get(int_lang, " ")

    def _replace_run(match: "re.Match") -> str:
        digit_run = match.group(0)
        return sep.join(words[int(d)] for d in digit_run)

    converted = _DIGIT_RUN_RE.sub(_replace_run, text)
    if converted != text:
        logger.info(f"数字转换 [{int_lang}]：'{text}' → '{converted}'")
    return converted


# ═════════════════════════════════════════════════════════════════════════════
# 对齐辅助音调偏移（"移调对齐"）
#   高音音频（如歌声跟读、高音色 TTS）在送入强制对齐模型时，偶发导致对齐
#   模型内部的语音特征提取出现错位（例如 Qwen3-FA 在处理高音/中英混合短
#   音频时，个别 token 的时间戳整体错序——详见 alt_aligners.py 里对
#   "退化区间"的说明）。这里提供一个可选的"对齐专用"移调预处理：只生成
#   一份音高整体升高/降低若干半音的临时音频副本，喂给对齐后端做时间戳
#   识别；识别完成后临时副本立即丢弃，不会写回任何落盘产物。
#
#   关键点：librosa.effects.pitch_shift 是"纯移调"（phase-vocoder 实现），
#   只改变基频，不改变音频时长——所以对齐后端返回的时间戳（无论是逐字符
#   还是逐词）可以直接原样使用，不需要任何时间轴换算或缩放，最终 LAB /
#   F0 曲线 / 工程文件仍然全部基于原始未处理的音频生成。这正是本功能被
#   设计为"仅影响送入对齐器的这一份临时拷贝"而不是全局重采样的原因。
# ═════════════════════════════════════════════════════════════════════════════

def _make_alignment_pitch_shifted_copy(src_wav_path: str, semitones: float) -> str:
    """
    读取 src_wav_path，整体移调 semitones 个半音后另存为临时 wav 文件，
    返回临时文件路径（调用方负责用完后删除）。semitones 为 0 时不会被
    调用方触发（各调用点均已提前判断），这里仍保留纯净的单一职责实现。
    """
    import tempfile
    import uuid as _uuid
    import librosa
    import soundfile as sf

    y, sr = librosa.load(src_wav_path, sr=None, mono=True)
    y = librosa.effects.pitch_shift(y, sr=sr, n_steps=semitones)

    tmp_path = os.path.join(
        tempfile.gettempdir(), f"_align_pitch_shift_{_uuid.uuid4().hex[:8]}.wav"
    )
    sf.write(tmp_path, y, sr, subtype="PCM_16")
    return tmp_path


def _release_gpu_resources_before_f0() -> None:
    """
    在"对齐阶段"结束、"F0 提取阶段"开始之前调用，尽量把对齐模型
    （Qwen3-FA / WhisperX / NeMo 等，均为 CUDA 常驻模型）占用的显存
    和 CUDA 上下文状态清理干净，再让 F0 提取（尤其 CREPE / RMVPE，
    同样需要往 GPU 加载模型）开始工作。

    背景：对齐模型和 F0 神经网络模型是两个独立生命周期的 CUDA 使用者，
    紧挨着连续执行时，前者的显存释放和后者的显存申请之间存在一个
    时间窗口——大多数情况下无害，但在部分 Windows + CUDA 驱动组合下
    曾观察到这个窗口内出现底层崩溃（非 Python 异常，np/torch 也无法
    捕获）。这里加一次显式的同步 + 缓存清理 + 垃圾回收，尽量缩小
    该窗口；即使当前环境用不到 CUDA（如纯 CPU 部署），也应安全跳过。

    全程做异常兜底，任何失败都不应该影响主流程——这只是一个尽力而为
    的稳定性加固，不是必需步骤。
    """
    try:
        import gc
        gc.collect()
    except Exception:
        pass

    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception as exc:
        logger.debug(f"[pipeline] 对齐后显存清理跳过（非致命）: {exc}")


def _run_alignment(
    audio_file,             # Flask FileStorage 或 FileStorageWrapper
    text: str,
    language: str,
    backend: str = "mfa",
    f0_device: str = "auto",
    whisperx_model: str = "large-v3",
    whisperx_batch_size: int = 16,      # 新增
    qwen3_batch_size: int = 8,          # 新增：Qwen3-ASR / Qwen3-FA / NeMo-FA 共用
    english_word_align: bool = False,
    nemo_model: Optional[str] = None,
    aligner_device: Optional[str] = None,   # 新增：对齐工具（WhisperX/Qwen3/NeMo）运行设备，
                                             # 与 f0_device（F0 提取设备，CREPE/RMVPE 专用）
                                             # 彻底解耦。None 时为向后兼容回退到 f0_device，
                                             # 但正常链路（app.py）应始终显式传入。
    align_pitch_shift_semitones: float = 0.0,   # 新增：对齐辅助移调（半音，正数升调/降调，
                                                 # 仅影响送入对齐后端的临时音频副本，不影响
                                                 # 最终 LAB 时间戳换算、F0 提取或工程文件音高）
    cancel_check: Optional[Callable[[], bool]] = None,       # ← 协作式取消：目前仅 MFA 分支
                                                              #   会在对齐运行期间轮询它
    on_process_start: Optional[Callable[[object], None]] = None,   # ← MFA 子进程句柄回调，
                                                                    #   供调用方登记以便直接 terminate
) -> Dict:
    """
    统一调度对齐后端，返回与 MFAProcessor.process() 格式兼容的字典。
    audio_file.save(path) 和 audio_file.filename 必须可用。
    """
    # 数字 → 读法文字转换，放在四个后端分支之前统一处理一次：
    # MFA / WhisperX / Qwen3-ASR / Qwen3-FA / NeMo-FA 都从这里拿到转换后的
    # 文本，不需要各自重复实现。text 为空（如 Qwen3-ASR 纯识别模式不提供
    # 参考文本）时 _convert_digits_to_words 内部直接原样返回，不受影响。
    text = _convert_digits_to_words(text, language)

    import tempfile, shutil, os

    if backend == "mfa":
        # MFA 走 Kaldi 音素级强制对齐，不像 Qwen3-FA/NeMo 那样直接对原始
        # 波形做端到端神经网络推理，历史上未观测到"高音导致错位"的问题；
        # 但既然移调预处理本身对时间戳无损，这里仍然统一支持，交由用户
        # 自行决定是否对 MFA 也启用（默认 0 半音即完全不生效，行为不变）。
        if not align_pitch_shift_semitones:
            processor = MFAProcessor()
            return processor.process(audio_file, text, language,
                                     english_word_align=english_word_align,
                                     on_process_start=on_process_start,
                                     cancel_check=cancel_check)

        tmp_dir = tempfile.mkdtemp(prefix="mfa_pitch_align_")
        try:
            filename = getattr(audio_file, "filename", "audio.wav")
            tmp_wav = os.path.join(tmp_dir, filename)
            audio_file.save(tmp_wav)
            shifted_path = _make_alignment_pitch_shifted_copy(
                tmp_wav, align_pitch_shift_semitones
            )
            try:
                processor = MFAProcessor()
                return processor.process(
                    _LocalFileAdapter(shifted_path), text, language,
                    english_word_align=english_word_align,
                    on_process_start=on_process_start,
                    cancel_check=cancel_check,
                )
            finally:
                if os.path.exists(shifted_path):
                    os.remove(shifted_path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── 替代后端 ──────────────────────────────────────────────────────────
    from alt_aligners import get_aligner

    # 【2026-07 修复】此前这里一直误用 f0_device（F0 音高提取设备，只有
    # advancedConfig.f0_method 为 crepe/rmvpe 时前端才会显示该控件，其余
    # 情况下前端根本不会提交这个字段，等于永远走默认值 "auto"）来决定
    # WhisperX / Qwen3 / NeMo 的运行设备——而前端实际展示给用户、用户真正
    # 用来选择"对齐工具运行设备"的是另一个独立控件 aligner_device。二者
    # 长期没有打通，导致用户在"对齐工具运行设备"里选择 CPU 完全不会生效，
    # 对齐后端仍按 f0_device 的（通常是默认 auto → 探测到的 cuda）运行。
    resolved_aligner_device = aligner_device if aligner_device is not None else f0_device

    # 把文件保存到临时目录，获取路径供 alt aligner 使用
    tmp_dir = tempfile.mkdtemp(prefix="alt_aligner_")
    try:
        filename = getattr(audio_file, "filename", "audio.wav")
        tmp_wav = os.path.join(tmp_dir, filename)
        audio_file.save(tmp_wav)

        # 对齐辅助移调：只替换喂给 aligner.align() 的音频路径，音频时长
        # 严格不变，因此返回的时间戳无需任何换算即可直接使用；原始
        # tmp_wav（未移调）在本函数作用域内不再被使用，但调用方
        # process_full / process_mfa_only 等仍然基于各自独立保存的原始
        # wav_path 做后续 F0 提取和工程文件生成，两者互不影响。
        align_target_path = tmp_wav
        shifted_path: Optional[str] = None
        if align_pitch_shift_semitones:
            shifted_path = _make_alignment_pitch_shifted_copy(
                tmp_wav, align_pitch_shift_semitones
            )
            align_target_path = shifted_path

        extra = {}
        if backend == "whisperx":
            extra["whisper_model"] = whisperx_model
            extra["batch_size"] = whisperx_batch_size   # 新增
        elif backend == "nemo_aligner":
            if nemo_model:
                extra["nemo_model"] = nemo_model
            extra["batch_size"] = qwen3_batch_size   # 新增（仅作为 OOM 自动降级日志参考值）
        elif backend in ("qwen3_asr", "qwen3_aligner"):
            extra["batch_size"] = qwen3_batch_size   # 新增
        aligner = get_aligner(backend, device=resolved_aligner_device, **extra)
        try:
            return aligner.align(align_target_path, text or None, language,
                                 english_word_align=english_word_align)
        finally:
            if shifted_path:
                try:
                    os.remove(shifted_path)
                except OSError:
                    pass
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


class AudioProcessingPipeline:
    """
    完整音频处理流程：
    1. 音频上传
    2. 对齐标注 (生成 LAB 文件)  ← 现在支持多后端
    3. 音高提取 (PyWORLD / CREPE / RMVPE)
    4. 工程文件生成 (SVP / USTX)
    """

    def __init__(self, work_dir: str):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.mfa_processor = MFAProcessor()
        self.tsubaki_processor = TsubakiProcessor(str(self.work_dir))

        logger.info(f"✓ 处理流程初始化，工作目录: {self.work_dir}")

    def process_full(
        self,
        audio_file,
        text: str,
        language: str = "cmn",
        output_format: str = "sv",
        project_title: str = "Project",
        bpm: float = 120.0,
        base_pitch: int = 60,
        f0_method: str = "dio",
        f0_smooth: bool = True,
        f0_smooth_window: int = 5,
        use_double_precision: bool = False,
        f0_floor: float = 71.0,
        f0_ceil: float = 800.0,
        refine_pitch: bool = False,
        export_pitch_line: bool = True,
        vsqx_pitch_smooth_window: int = 5,           # ← VSQX PIT 曲线平滑窗口
        f0_device: str = "auto",
        crepe_model: str = "full",
        aligner_backend: str = "mfa",           # ← 新增
        aligner_device: Optional[str] = None,    # ← 对齐工具运行设备（与 f0_device 解耦，None 时回退到 f0_device）
        whisperx_model: str = "large-v3",       # ← WhisperX 模型大小
        whisperx_batch_size: int = 16,           # ← WhisperX 推理批大小（仅 device=cuda 时有意义）
        qwen3_batch_size: int = 8,                # ← Qwen3-ASR/Qwen3-FA/NeMo-FA 共用批大小设置
        nemo_model: Optional[str] = None,        # ← NeMo Forced Aligner 模型覆盖（可选）
        english_word_align: bool = False,        # ← 英语单词级对齐
        vsqx_singer: str = "MIKU_V4_Chinese",           # ← VSQX 声库名（由 app.py 按语种注入）
        vsqx_singer_id: str = "BNGE7CP7EMTRSNC3",       # ← VSQX 声库 ID
        vsqx_singer_bs: int = 4,                         # ← VSQX 声库 Bank Select（VOCALOID4 内部编号）
        word_phoneme_map: bool = False,                  # ← 英语单词 → 音素写入（SVP/VSQX）
        dict_source: str = "default",                     # ← 单词→音素词典来源
        align_pitch_shift_semitones: float = 0.0,        # ← 对齐辅助移调（半音），不影响最终产物音高
        cancel_check: Optional[Callable[[], bool]] = None,   # ← 协作式取消：每个阶段边界调用一次，
                                                              #   返回 True 时提前中止并返回 stage="cancelled"
        on_process_start: Optional[Callable[[object], None]] = None,   # ← MFA 子进程句柄回调，
                                                                        #   供调用方登记以便直接 terminate
    ) -> Dict:
        def _cancelled(stage: str, processing_time: int) -> Dict:
            logger.info(f"⏹ 任务已取消 (阶段: {stage})")
            return {
                "success": False, "error": "用户已取消",
                "stage": "cancelled", "cancelled_at_stage": stage,
                "processing_time": processing_time,
            }

        config = AudioProcessingConfig(
            bpm=bpm,
            base_pitch=base_pitch,
            f0_method=f0_method,
            f0_smooth=f0_smooth,
            f0_smooth_window=f0_smooth_window,
            refine_pitch=refine_pitch,
            export_pitch_line=export_pitch_line,
            vsqx_pitch_smooth_window=vsqx_pitch_smooth_window,
            use_double_precision=use_double_precision,
            f0_floor=f0_floor,
            f0_ceil=f0_ceil,
            f0_device=f0_device,
            crepe_model=crepe_model,
        )

        import time
        start_time = time.time()

        try:
            logger.info("=" * 60)
            logger.info(f"开始完整处理流程 [aligner={aligner_backend}]")
            logger.info("=" * 60)

            audio_filename = getattr(audio_file, "filename", "audio")
            stem = Path(audio_filename).stem
            wav_path = str(self.work_dir / f"{stem}.wav")

            logger.info(f"正在保存音频: {wav_path}")
            Path(wav_path).parent.mkdir(parents=True, exist_ok=True)
            audio_file.seek(0)
            audio_file.save(wav_path)
            audio_file.seek(0)

            if cancel_check and cancel_check():
                return _cancelled("alignment", int((time.time() - start_time) * 1000))

            # ── 步骤 1：对齐标注 ──────────────────────────────────────────
            logger.info(f"[ 步骤 1/3 ] 对齐标注 (backend={aligner_backend})...")
            align_result = _run_alignment(audio_file, text, language, aligner_backend,
                                           f0_device, whisperx_model,
                                           whisperx_batch_size=whisperx_batch_size,
                                           qwen3_batch_size=qwen3_batch_size,
                                           english_word_align=english_word_align,
                                           nemo_model=nemo_model,
                                           aligner_device=aligner_device,
                                           align_pitch_shift_semitones=align_pitch_shift_semitones,
                                           cancel_check=cancel_check,
                                           on_process_start=on_process_start)
            if align_result.get("stage") == "cancelled":
                return _cancelled("alignment", int((time.time() - start_time) * 1000))
            if not align_result.get("success"):
                error = align_result.get("error", "对齐处理失败")
                logger.error(f"✗ 对齐失败: {error}")
                return {
                    "success": False, "error": error,
                    "stage": "alignment",
                    "processing_time": int((time.time() - start_time) * 1000),
                }

            lab_content = align_result.get("lab_content", "")
            lab_path = str(self.work_dir / f"{stem}.lab")
            with open(lab_path, "w", encoding="utf-8") as f:
                f.write(lab_content)
            logger.info(f"✓ LAB 标注完成: {lab_path}")

            if cancel_check and cancel_check():
                return _cancelled("f0_extraction", int((time.time() - start_time) * 1000))

            # ── 对齐模型（Qwen3-FA/WhisperX/NeMo 等）与 F0 模型（CREPE/
            #    RMVPE）都是独立的 CUDA 常驻模型，紧邻执行前先清理一次，
            #    避免显存释放/申请的时间窗口重叠导致底层不稳定 ──────────
            _release_gpu_resources_before_f0()

            # ── 步骤 2：F0 提取（注意：始终基于步骤开头保存的原始 wav_path，
            #    不受 align_pitch_shift_semitones 影响——移调只发生在对齐
            #    阶段内部的临时副本上，此处 wav_path 从未被替换过）───────
            logger.info("[ 步骤 2/3 ] 音高提取...")
            try:
                audio_data = self.tsubaki_processor.process_audio_f0(wav_path, config)
                if not audio_data or not audio_data.get("success"):
                    logger.warning(
                        f"⚠ F0 提取失败({(audio_data or {}).get('error', 'unknown')})，继续（不含音高曲线）"
                    )
                    audio_data = None
            except Exception as e:
                logger.warning(f"⚠ 音高提取异常: {e}，继续生成工程文件")
                audio_data = None
            logger.info("✓ 音高提取完成")

            if cancel_check and cancel_check():
                return _cancelled("project_generation", int((time.time() - start_time) * 1000))

            # ── 步骤 3：生成工程文件 ─────────────────────────────────────
            logger.info(f"[ 步骤 3/3 ] 生成 {output_format.upper()} 工程文件...")
            project_result = self.tsubaki_processor.process_full_pipeline(
                wav_path=wav_path,
                lab_path=lab_path,
                output_format=output_format,
                project_title=project_title,
                config=config,
                audio_f0_data=audio_data,
                vsqx_singer=vsqx_singer,
                vsqx_singer_id=vsqx_singer_id,
                vsqx_singer_bs=vsqx_singer_bs,
                word_phoneme_map=word_phoneme_map,
                language=language,
                original_text=text,        # ← 原始汉字/韩文文本，用于预提取真实英语单词
                dict_source=dict_source,
            )

            if not project_result.get("success"):
                error = project_result.get("error", "工程文件生成失败")
                logger.error(f"✗ 工程文件生成失败: {error}")
                return {
                    "success": False, "error": error,
                    "stage": "project_generation",
                    "lab_path": lab_path,
                    "processing_time": int((time.time() - start_time) * 1000),
                }

            project_path = project_result.get("output_path")
            processing_time = int((time.time() - start_time) * 1000)

            logger.info("=" * 60)
            logger.info("✓ 完整处理流程完成")
            logger.info(f" LAB 文件: {lab_path}")
            logger.info(f" 工程文件: {project_path}")
            logger.info(f" 耗时: {processing_time}ms")
            logger.info("=" * 60)

            return {
                "success": True,
                "lab_path": lab_path,
                "lab_content": lab_content,
                "project_path": project_path,
                "project_format": project_result.get("format", output_format),
                "requested_format": output_format,
                "segments": project_result.get("segments", 0),
                "processing_time": processing_time,
                "config": config.to_dict(),
                "aligner_backend": aligner_backend,
                "message": f'完整处理完成: {project_result.get("segments", 0)} 个标注段',
            }
        except Exception as e:
            logger.error(f"✗ 处理流程异常: {e}", exc_info=True)
            return {
                "success": False, "error": str(e), "stage": "unknown",
                "processing_time": int((time.time() - start_time) * 1000),
            }

    def process_mfa_only(
        self,
        audio_file,
        text: str,
        language: str = "cmn",
        aligner_backend: str = "mfa",           # ← 新增
        f0_device: str = "auto",
        aligner_device: Optional[str] = None,    # ← 对齐工具运行设备（与 f0_device 解耦，None 时回退到 f0_device）
        whisperx_model: str = "large-v3",       # ← WhisperX 模型大小
        whisperx_batch_size: int = 16,           # ← WhisperX 推理批大小（仅 device=cuda 时有意义）
        qwen3_batch_size: int = 8,                # ← Qwen3-ASR/Qwen3-FA/NeMo-FA 共用批大小设置
        nemo_model: Optional[str] = None,        # ← NeMo Forced Aligner 模型覆盖（可选）
        english_word_align: bool = False,        # ← 英语单词级对齐
        align_pitch_shift_semitones: float = 0.0,   # ← 对齐辅助移调（半音），不影响 LAB 时间戳换算
        cancel_check: Optional[Callable[[], bool]] = None,       # ← 协作式取消（MFA 分支运行期间轮询）
        on_process_start: Optional[Callable[[object], None]] = None,   # ← MFA 子进程句柄回调
    ) -> Dict:
        """仅执行对齐标注（不生成工程文件）"""
        import time
        start_time = time.time()

        try:
            logger.info(f"[ 标注模式 ] 执行自动标注 (backend={aligner_backend})")

            result = _run_alignment(audio_file, text, language, aligner_backend,
                                    f0_device, whisperx_model,
                                    whisperx_batch_size=whisperx_batch_size,
                                    qwen3_batch_size=qwen3_batch_size,
                                    english_word_align=english_word_align,
                                    nemo_model=nemo_model,
                                    aligner_device=aligner_device,
                                    align_pitch_shift_semitones=align_pitch_shift_semitones,
                                    cancel_check=cancel_check,
                                    on_process_start=on_process_start)

            if result.get("stage") == "cancelled":
                return {
                    **result,
                    "processing_time": int((time.time() - start_time) * 1000),
                    "aligner_backend": aligner_backend,
                }

            if result.get("success"):
                lab_content = result.get("lab_content", "")
                audio_filename = getattr(audio_file, "filename", "audio")
                stem = Path(audio_filename).stem
                lab_path = str(self.work_dir / f"{stem}.lab")

                Path(lab_path).parent.mkdir(parents=True, exist_ok=True)
                with open(lab_path, "w", encoding="utf-8") as f:
                    f.write(lab_content)

                logger.info(f"✓ LAB 标注已保存: {lab_path}")
                result["lab_path"] = lab_path
                result["processing_time"] = int((time.time() - start_time) * 1000)
                result["aligner_backend"] = aligner_backend
                return result
            else:
                return {
                    **result,
                    "processing_time": int((time.time() - start_time) * 1000),
                    "aligner_backend": aligner_backend,
                }

        except Exception as e:
            logger.error(f"✗ 标注处理异常: {e}", exc_info=True)
            return {
                "success": False, "error": str(e),
                "processing_time": int((time.time() - start_time) * 1000),
            }

    def process_project_only(
        self,
        wav_path: str,
        lab_path: Optional[str] = None,
        output_format: str = "sv",
        project_title: str = "Project",
        bpm: float = 120.0,
        base_pitch: int = 60,
        f0_method: str = "dio",
        f0_smooth: bool = True,
        f0_smooth_window: int = 5,
        use_double_precision: bool = False,
        f0_floor: float = 71.0,
        f0_ceil: float = 800.0,
        refine_pitch: bool = False,
        export_pitch_line: bool = True,
        vsqx_pitch_smooth_window: int = 5,           # ← VSQX PIT 曲线平滑窗口
        f0_device: str = "auto",
        crepe_model: str = "full",
        phoneme_mode: str = "none",
        midi_path: str = None,
        lyrics_text: str = "",
        vsqx_singer: str = "MIKU_V4X_Original_EVEC",    # ← 仅生成工程默认日语声库
        vsqx_singer_id: str = "BCNFCY43LB2LZCD4",       # ← 对应声库 ID
        vsqx_singer_bs: int = 0,                          # ← 仅生成工程默认 bs（日语声库=0）
        word_phoneme_map: bool = False,                   # ← 英语单词 → 音素写入（SVP/VSQX）
        language: str = "",                               # ← 语种，传给构建器防误判
        original_text: str = "",                          # ← 原始歌词文本（汉字/韩文），用于预提取英语单词
        dict_source: str = "default",                      # ← 单词→音素词典来源
        ja_devoiced_phoneme: bool = False,                      # ← 日语辅音起始音素锁定（<p lock="1">）
    ) -> Dict:
        """仅执行工程文件生成（已有 WAV 以及 LAB/MIDI 之一）"""
        import time
        start_time = time.time()

        try:
            logger.info("[ 工程文件模式 ] 生成项目文件")
            logger.info(f" 音频: {wav_path}")
            logger.info(f" 标注: {lab_path or '(无 LAB)'}")
            logger.info(f" MIDI: {midi_path or '(无 MIDI)'}")
            logger.info(f" 格式: {output_format}")

            if not Path(wav_path).exists():
                return {
                    "success": False, "error": f"WAV 文件不存在: {wav_path}",
                    "processing_time": 0,
                }

            lab_exists  = bool(lab_path  and Path(lab_path).exists())
            midi_exists = bool(midi_path and Path(midi_path).exists())

            if not lab_exists and not midi_exists:
                return {
                    "success": False,
                    "error": "需要 LAB 文件或 MIDI 文件（至少提供其中一个）",
                    "processing_time": 0,
                }

            config = AudioProcessingConfig(
                bpm=bpm, base_pitch=base_pitch,
                f0_floor=f0_floor, f0_ceil=f0_ceil,
                f0_method=f0_method, f0_smooth=f0_smooth,
                f0_smooth_window=f0_smooth_window,
                use_double_precision=use_double_precision,
                refine_pitch=refine_pitch,
                export_pitch_line=export_pitch_line,
                vsqx_pitch_smooth_window=vsqx_pitch_smooth_window,
                f0_device=f0_device, crepe_model=crepe_model,
            )

            audio_data = None
            try:
                _release_gpu_resources_before_f0()
                audio_data = self.tsubaki_processor.process_audio_f0(wav_path, config)
                if not audio_data or not audio_data.get("success"):
                    logger.warning(
                        f"⚠ F0 提取失败({(audio_data or {}).get('error', 'unknown')})，继续生成工程文件"
                    )
                    audio_data = None
            except Exception as e:
                logger.warning(f"⚠ 音高提取异常: {e}，继续生成工程文件")

            result = self.tsubaki_processor.process_full_pipeline(
                wav_path=wav_path,
                lab_path=lab_path if lab_exists else None,
                output_format=output_format,
                project_title=project_title,
                config=config,
                audio_f0_data=audio_data,
                phoneme_mode=phoneme_mode,
                midi_path=midi_path or None,
                lyrics_text=lyrics_text,
                vsqx_singer=vsqx_singer,
                vsqx_singer_id=vsqx_singer_id,
                vsqx_singer_bs=vsqx_singer_bs,
                word_phoneme_map=word_phoneme_map,
                language=language,
                original_text=original_text,   # ← 透传原始文本（仅生成工程模式下可选提供）
                dict_source=dict_source,
                ja_devoiced_phoneme=ja_devoiced_phoneme,
            )

            result["processing_time"] = int((time.time() - start_time) * 1000)
            return result

        except Exception as e:
            logger.error(f"✗ 工程文件生成异常: {e}", exc_info=True)
            return {
                "success": False, "error": str(e),
                "processing_time": int((time.time() - start_time) * 1000),
            }

    # ═════════════════════════════════════════════════════════════════════
    # 对话文本框批量处理（功能 3）
    # ═════════════════════════════════════════════════════════════════════

    def process_dialogue_batch(
        self,
        boxes: List[Dict],
        language: str = "cmn",
        output_format: str = "sv",
        project_title: str = "Dialogue Project",
        bpm: float = 120.0,
        base_pitch: int = 60,
        f0_method: str = "dio",
        f0_smooth: bool = True,
        f0_smooth_window: int = 5,
        use_double_precision: bool = False,
        f0_floor: float = 71.0,
        f0_ceil: float = 800.0,
        refine_pitch: bool = False,
        export_pitch_line: bool = True,
        vsqx_pitch_smooth_window: int = 5,           # ← VSQX PIT 曲线平滑窗口
        f0_device: str = "auto",
        crepe_model: str = "full",
        aligner_backend: str = "mfa",
        aligner_device: Optional[str] = None,    # ← 对齐工具运行设备（与 f0_device 解耦，None 时回退到 f0_device）
        whisperx_model: str = "large-v3",
        whisperx_batch_size: int = 16,           # ← WhisperX 推理批大小（仅 device=cuda 时有意义）
        qwen3_batch_size: int = 8,                # ← Qwen3-ASR/Qwen3-FA/NeMo-FA 共用批大小设置
        nemo_model: Optional[str] = None,
        english_word_align: bool = False,
        vsqx_singer: str = "MIKU_V4_Chinese",
        vsqx_singer_id: str = "BNGE7CP7EMTRSNC3",
        vsqx_singer_bs: int = 4,
        word_phoneme_map: bool = False,
        dict_source: str = "default",
        processing_mode: str = "full",
        phoneme_mode: str = "none",
        ja_devoiced_phoneme: bool = False,   # ← 日语辅音起始音素锁定（<p lock="1">），透传给
                                          #   build_multitrack_project；仅 vsqx 输出生效
        align_pitch_shift_semitones: float = 0.0,   # ← 兼容旧调用方的全局默认值；现推荐通过每个
                                                     #   box 自带的 "align_pitch_shift_semitones"
                                                     #   字段传入，per-box 优先，此参数仅在该字段
                                                     #   缺失时兜底
        progress_cb: Optional[Callable[[int, int, Dict], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,   # ← 协作式取消：每处理完
                                                              #   一个对话框检查一次
        on_process_start: Optional[Callable[[object], None]] = None,   # ← MFA 子进程句柄回调
                                                                        #   （某个对话框走 MFA 对齐时）
    ) -> Dict:
        """
        对话文本框批量处理入口：逐个对话框顺序处理（对齐/LAB/MIDI 导入 + 音高提取），
        最终把全部成功的对话框写入*同一个*单音轨工程文件（而不是分别导出
        多个独立工程文件，也不再为每个对话框生成独立音轨）——但每个对话框
        仍是该音轨下一段独立的序列（SVP group / USTX voice_part / VSQX
        vsPart），只按顺序"背靠背"定位到时间轴上，*不会*把多个对话框的
        音符拍平合并进同一段序列：第 i 个对话框所在序列的起始时间 = 排在
        它前面的全部对话框的真实音频时长之和（音频推断的分段起始时间，
        无缝衔接），详见 build_multitrack_project。

        顶部"高级设置"（对齐后端 / 语种 / BPM / F0 参数 / word_phoneme_map /
        dict_source 等）对全部对话框统一生效，与单文件处理页面语义一致。

        Parameters
        ----------
        boxes : 每个元素描述一个对话框，按前端展示顺序排列（决定其在合并后
            时间轴上的先后位置）：
            {
                "index": int,                  # 对话框序号（用于结果回填，从 0 开始）
                "text": str,                    # 台词文本（可选；MFA/Qwen3-FA/NeMo-FA 需要）
                "audio_path": Optional[str],    # 已保存到磁盘的音频路径；缺失则整框跳过
                "lab_path": Optional[str],      # 已保存到磁盘的 LAB 路径；提供时跳过对齐，直接使用
                                                 # （LAB 优先级高于 MIDI，两者都提供时以 LAB 为准）
                "midi_path": Optional[str],     # 已保存到磁盘的 MIDI 路径；无 LAB 时提供则跳过对齐，
                                                 # 从 MIDI 音符自动生成段落（并读取 MIDI BPM/音高）
                "align_pitch_shift_semitones": Optional[float],  # 该对话框自己的对齐辅助移调
                                                 # （半音），每框独立生效，仅在该框走对齐后端时
                                                 # 使用；缺失时按 0（不移调）处理。
            }
        processing_mode : "full"（完整处理：对齐 + F0 提取 + 工程文件生成，
            没有 LAB/MIDI 的对话框会走 aligner_backend 对齐）或
            "project-only"（仅生成工程：跳过对齐，只使用已提供的 WAV + LAB/MIDI；
            既没有 LAB 也没有 MIDI 的对话框直接跳过，不报错中断整体，
            与前端"该对话框跳过"的约定一致）。
        phoneme_mode : "none"/"merge"/"hiragana"/"katakana"，透传给
            build_multitrack_project（仅对来自 LAB 的段落、且输出格式非
            USTX 时生效；MIDI 来源的段落与 USTX 格式不受影响）。
        progress_cb : 可选回调 (done_count, total_count, box_result)，
            每处理完一个对话框调用一次，供路由层更新任务进度供前端轮询。

        Returns
        -------
        Dict : {
            "success": bool,
            "project_path": str,        # 仅在至少一个对话框成功时存在
            "project_format": str,
            "boxes": List[Dict],        # 每个对话框的处理结果（status: done/failed/skipped_empty）
            "processed_count": int,     # status == "done"
            "failed_count": int,
            "skipped_count": int,
            "processing_time": int,     # 毫秒
        }
        """
        import time
        from dataclasses import replace as _dc_replace
        start_time = time.time()

        if processing_mode not in ("full", "project-only"):
            processing_mode = "full"

        config = AudioProcessingConfig(
            bpm=bpm,
            base_pitch=base_pitch,
            f0_method=f0_method,
            f0_smooth=f0_smooth,
            f0_smooth_window=f0_smooth_window,
            refine_pitch=refine_pitch,
            export_pitch_line=export_pitch_line,
            vsqx_pitch_smooth_window=vsqx_pitch_smooth_window,
            use_double_precision=use_double_precision,
            f0_floor=f0_floor,
            f0_ceil=f0_ceil,
            f0_device=f0_device,
            crepe_model=crepe_model,
        )

        total = len(boxes)
        results: List[Dict] = []
        track_inputs: List[Dict] = []

        logger.info("=" * 60)
        logger.info(
            f"开始对话文本框批量处理 [共 {total} 个对话框, "
            f"mode={processing_mode}, backend={aligner_backend if processing_mode == 'full' else '(跳过对齐)'}]"
        )
        logger.info("=" * 60)

        for i, box in enumerate(boxes):
            idx = box.get("index", i)
            audio_path = box.get("audio_path")
            lab_path_in = box.get("lab_path")
            midi_path_in = box.get("midi_path")
            text = (box.get("text") or "").strip()
            # 每框独立的对齐辅助移调：优先使用该框自带的值；缺失时（如
            # 旧版调用方仍只传统一的 align_pitch_shift_semitones 参数）
            # 回退到方法级默认值，保持向后兼容。
            box_align_pitch_shift_semitones = box.get(
                "align_pitch_shift_semitones", align_pitch_shift_semitones
            )

            # ── 该对话框的"单独设置"覆盖值（对齐后端/语言/英语单词级对齐/
            # 词典/音素转换/F0 高级设置，不含 BPM）：未开启覆盖（override
            # 为 None/空）时，下面每个字段都直接回退到整批统一的全局参数，
            # 与该功能上线前的行为完全一致；开启时，只有这一个对话框使用
            # 覆盖值，不影响其它对话框。────────────────────────────────────
            box_override = box.get("override") or {}
            box_aligner_backend      = box_override.get("aligner_backend", aligner_backend)
            box_language             = box_override.get("language", language)
            box_english_word_align   = box_override.get("english_word_align", english_word_align)
            box_word_phoneme_map     = box_override.get("word_phoneme_map", word_phoneme_map)
            box_phoneme_mode         = box_override.get("phoneme_mode", phoneme_mode)
            box_dict_source          = box_override.get("dict_source", dict_source)
            box_ja_devoiced_phoneme      = bool(box_override.get("ja_devoiced_phoneme", ja_devoiced_phoneme))

            # 该对话框自己的 F0/音高相关配置：只替换 override 里显式提供的
            # 字段，bpm 恒定沿用全局 config（见函数 docstring 里 BPM 的说明），
            # 未开启覆盖时 box_config 与全局 config 完全一致（等价于不做
            # dataclasses.replace）。
            if box_override:
                box_config = _dc_replace(
                    config,
                    base_pitch=box_override.get("base_pitch", config.base_pitch),
                    refine_pitch=box_override.get("auto_note_pitch", config.refine_pitch),
                    export_pitch_line=box_override.get("export_pitch_line", config.export_pitch_line),
                    f0_method=box_override.get("f0_method", config.f0_method),
                    f0_device=box_override.get("f0_device", config.f0_device),
                    crepe_model=box_override.get("crepe_model", config.crepe_model),
                    use_double_precision=box_override.get("use_double_precision", config.use_double_precision),
                    f0_smooth=box_override.get("f0_smooth", config.f0_smooth),
                    f0_smooth_window=box_override.get("f0_smooth_window", config.f0_smooth_window),
                    vsqx_pitch_smooth_window=box_override.get("vsqx_pitch_smooth_window", config.vsqx_pitch_smooth_window),
                    f0_floor=box_override.get("f0_floor", config.f0_floor),
                    f0_ceil=box_override.get("f0_ceil", config.f0_ceil),
                )
            else:
                box_config = config

            box_result: Dict = {"index": idx}

            if not audio_path or not Path(str(audio_path)).exists():
                box_result.update(status="skipped_empty", message="未提供音频")
                results.append(box_result)
                if progress_cb:
                    progress_cb(i + 1, total, box_result)
                continue

            lab_provided  = bool(lab_path_in  and Path(str(lab_path_in)).exists())
            midi_provided = bool(midi_path_in and Path(str(midi_path_in)).exists())

            # ── 「仅生成工程」模式：既没有 LAB 也没有 MIDI 的对话框直接跳过
            #    （不报错中断整体，仅标记该框为 skipped_empty）──────────────
            if processing_mode == "project-only" and not lab_provided and not midi_provided:
                box_result.update(status="skipped_empty", message="未提供 LAB / MIDI 文件（仅生成工程模式需要其一）")
                results.append(box_result)
                if progress_cb:
                    progress_cb(i + 1, total, box_result)
                continue

            try:
                stem = f"dlg_{idx:03d}_{Path(str(audio_path)).stem}"
                wav_dst = self.work_dir / f"{stem}.wav"
                if os.path.abspath(str(wav_dst)) != os.path.abspath(str(audio_path)):
                    shutil.copy(str(audio_path), str(wav_dst))
                wav_path_final = str(wav_dst)

                lab_path_final = None
                midi_path_final = None

                if lab_provided:
                    # ── LAB 导入模式：跳过对齐，直接使用现成 LAB（最高优先级）──
                    lab_dst = self.work_dir / f"{stem}.lab"
                    shutil.copy(str(lab_path_in), str(lab_dst))
                    lab_path_final = str(lab_dst)
                    if midi_provided:
                        # LAB 优先，但仍保留 MIDI 路径供音高/BPM 参考
                        midi_dst = self.work_dir / f"{stem}.mid"
                        shutil.copy(str(midi_path_in), str(midi_dst))
                        midi_path_final = str(midi_dst)
                elif midi_provided:
                    # ── MIDI 导入模式：跳过对齐，从 MIDI 音符自动生成段落 ──
                    midi_dst = self.work_dir / f"{stem}.mid"
                    shutil.copy(str(midi_path_in), str(midi_dst))
                    midi_path_final = str(midi_dst)
                else:
                    # ── 无 LAB/MIDI：走对齐后端（仅 "full" 模式会走到这里，
                    #    "project-only" 模式已在前面被跳过）──────────────────
                    text_optional = box_aligner_backend in ("whisperx", "qwen3_asr")
                    if not text and not text_optional:
                        box_result.update(
                            status="failed",
                            error="该对齐后端需要参考文本（MFA / Qwen3-ForcedAligner / NeMo Forced Aligner）",
                        )
                        results.append(box_result)
                        if progress_cb:
                            progress_cb(i + 1, total, box_result)
                        continue

                    audio_adapter = _LocalFileAdapter(wav_path_final)
                    align_result = _run_alignment(
                        audio_adapter, text, box_language, box_aligner_backend,
                        f0_device, whisperx_model,
                        whisperx_batch_size=whisperx_batch_size,
                        qwen3_batch_size=qwen3_batch_size,
                        english_word_align=box_english_word_align,
                        nemo_model=nemo_model,
                        aligner_device=aligner_device,
                        align_pitch_shift_semitones=box_align_pitch_shift_semitones,
                        cancel_check=cancel_check,
                        on_process_start=on_process_start,
                    )
                    if align_result.get("stage") == "cancelled":
                        logger.info(f"⏹ 批量处理已取消（对话框 #{idx} 对齐阶段）")
                        processed_count = sum(1 for r in results if r.get("status") == "done")
                        failed_count    = sum(1 for r in results if r.get("status") == "failed")
                        skipped_count   = sum(1 for r in results if r.get("status") == "skipped_empty")
                        return {
                            "success": False, "error": "用户已取消",
                            "stage": "cancelled",
                            "boxes": results,
                            "processed_count": processed_count,
                            "failed_count": failed_count,
                            "skipped_count": skipped_count,
                            "processing_time": int((time.time() - start_time) * 1000),
                        }
                    if not align_result.get("success"):
                        box_result.update(
                            status="failed",
                            error=align_result.get("error", "对齐处理失败"),
                        )
                        results.append(box_result)
                        if progress_cb:
                            progress_cb(i + 1, total, box_result)
                        continue

                    lab_content = align_result.get("lab_content", "")
                    lab_dst = self.work_dir / f"{stem}.lab"
                    lab_dst.write_text(lab_content, encoding="utf-8")
                    lab_path_final = str(lab_dst)

                track_title = (text[:24].strip() if text else "") or Path(str(audio_path)).stem or f"Track {idx + 1}"

                track_inputs.append({
                    "title": track_title,
                    "wav_path": wav_path_final,
                    "lab_path": lab_path_final,
                    "midi_path": midi_path_final,
                    "original_text": text,
                    # ── 该对话框自己的有效设置（覆盖值或回退后的全局值），
                    # 供 build_multitrack_project 按每个音轨独立应用词典/
                    # 单词映射/音素转换/F0 配置，而不是整批统一一份。────
                    "config": box_config,
                    "language": box_language,
                    "word_phoneme_map": box_word_phoneme_map,
                    "dict_source": box_dict_source,
                    "phoneme_mode": box_phoneme_mode,
                    "ja_devoiced_phoneme": box_ja_devoiced_phoneme,
                })

                box_result.update(
                    status="done", wav_path=wav_path_final,
                    lab_path=lab_path_final, midi_path=midi_path_final,
                )
                results.append(box_result)

            except Exception as e:
                logger.error(f"✗ 对话框 #{idx} 处理异常: {e}", exc_info=True)
                box_result.update(status="failed", error=str(e))
                results.append(box_result)

            if progress_cb:
                progress_cb(i + 1, total, box_result)

            if cancel_check and cancel_check():
                logger.info(f"⏹ 批量处理已取消（已处理 {i + 1}/{total} 个对话框）")
                processed_count = sum(1 for r in results if r.get("status") == "done")
                failed_count    = sum(1 for r in results if r.get("status") == "failed")
                skipped_count   = sum(1 for r in results if r.get("status") == "skipped_empty")
                return {
                    "success": False, "error": "用户已取消",
                    "stage": "cancelled",
                    "boxes": results,
                    "processed_count": processed_count,
                    "failed_count": failed_count,
                    "skipped_count": skipped_count,
                    "processing_time": int((time.time() - start_time) * 1000),
                }

        processed_count = sum(1 for r in results if r.get("status") == "done")
        failed_count    = sum(1 for r in results if r.get("status") == "failed")
        skipped_count   = sum(1 for r in results if r.get("status") == "skipped_empty")

        if not track_inputs:
            return {
                "success": False,
                "error": "没有任何对话框成功处理，无法生成工程文件",
                "boxes": results,
                "processed_count": processed_count,
                "failed_count": failed_count,
                "skipped_count": skipped_count,
                "processing_time": int((time.time() - start_time) * 1000),
            }

        if cancel_check and cancel_check():
            logger.info("⏹ 批量处理已取消（所有对话框已处理完毕，工程文件生成前）")
            return {
                "success": False, "error": "用户已取消",
                "stage": "cancelled",
                "boxes": results,
                "processed_count": processed_count,
                "failed_count": failed_count,
                "skipped_count": skipped_count,
                "processing_time": int((time.time() - start_time) * 1000),
            }

        # 所有对话框的对齐阶段（可能含 Qwen3-FA/WhisperX/NeMo 等 CUDA 常驻
        # 模型）已全部结束，build_multitrack_project 内部会对每个音轨做
        # F0 提取（可能是 CREPE/RMVPE，同样需要 CUDA）——紧邻执行前先清理，
        # 避免显存释放/申请窗口重叠。
        _release_gpu_resources_before_f0()

        project_result = self.tsubaki_processor.build_multitrack_project(
            project_title=project_title,
            track_inputs=track_inputs,
            output_format=output_format,
            config=config,
            word_phoneme_map=word_phoneme_map,
            language=language,
            dict_source=dict_source,
            vsqx_singer=vsqx_singer,
            vsqx_singer_id=vsqx_singer_id,
            vsqx_singer_bs=vsqx_singer_bs,
            phoneme_mode=phoneme_mode,
            ja_devoiced_phoneme=ja_devoiced_phoneme,
        )

        processing_time = int((time.time() - start_time) * 1000)

        if not project_result.get("success"):
            return {
                "success": False,
                "error": project_result.get("error", "批量顺序合并工程文件生成失败"),
                "boxes": results,
                "processed_count": processed_count,
                "failed_count": failed_count,
                "skipped_count": skipped_count,
                "processing_time": processing_time,
            }

        logger.info("=" * 60)
        logger.info("✓ 对话文本框批量处理完成")
        logger.info(f" 工程文件: {project_result.get('output_path')}")
        logger.info(f" 成功/失败/跳过: {processed_count}/{failed_count}/{skipped_count}")
        logger.info(f" 耗时: {processing_time}ms")
        logger.info("=" * 60)

        return {
            "success":         True,
            "project_path":    project_result.get("output_path"),
            "project_format":  project_result.get("format", output_format),
            "requested_format": output_format,
            "track_count":     project_result.get("track_count", len(track_inputs)),
            "boxes":           results,
            "processed_count": processed_count,
            "failed_count":    failed_count,
            "skipped_count":   skipped_count,
            "processing_time": processing_time,
            "message": f"对话文本框批量处理完成: {processed_count} 个音轨已合并写入工程文件",
        }

    def process_f0_only(
        self,
        wav_path: str,
        method: str = "dio",
        f0_floor: float = 71.0,
        f0_ceil: float = 800.0,
        f0_smooth: bool = True,
        f0_smooth_window: int = 5,
        use_double_precision: bool = False,
        f0_device: str = "auto",
        crepe_model: str = "full",
    ) -> Dict:
        """仅执行 F0 提取"""
        import time
        start_time = time.time()
        try:
            logger.info(f"[ F0 模式 ] 提取音高 (method={method})")
            config = AudioProcessingConfig(
                f0_method=method, f0_floor=f0_floor, f0_ceil=f0_ceil,
                f0_smooth=f0_smooth, f0_smooth_window=f0_smooth_window,
                use_double_precision=use_double_precision,
                f0_device=f0_device, crepe_model=crepe_model,
            )
            audio_data = self.tsubaki_processor.process_audio_f0(wav_path, config)
            if audio_data and audio_data.get("success"):
                logger.info("✓ F0 提取完成")
                return {
                    "success": True, "method": method,
                    "frames": len(audio_data.get("f0", [])),
                    "sample_rate": audio_data.get("sr", 0),
                    "processing_time": int((time.time() - start_time) * 1000),
                    "message": f'F0 提取完成: {len(audio_data.get("f0", []))} 帧',
                }
            else:
                return {
                    "success": False,
                    "error": (audio_data or {}).get("error", "F0 提取失败"),
                    "processing_time": int((time.time() - start_time) * 1000),
                }
        except Exception as e:
            logger.error(f"✗ F0 提取异常: {e}", exc_info=True)
            return {
                "success": False, "error": str(e),
                "processing_time": int((time.time() - start_time) * 1000),
            }

    def get_supported_formats(self) -> Dict:
        return {
            "formats": list(self.tsubaki_processor.SUPPORTED_FORMATS.keys()),
            "details": self.tsubaki_processor.SUPPORTED_FORMATS,
        }

    def get_status(self) -> Dict:
        from mfa_utils import MFAChecker
        from f0_extractors import get_f0_backend_status
        from alt_aligners import get_alt_aligner_status

        mfa_status = MFAChecker.get_status()
        pyworld_available = self.tsubaki_processor.process_audio_f0.__globals__.get("pw") is not None

        return {
            "initialized": True,
            "work_dir": str(self.work_dir),
            "mfa": mfa_status,
            "audio_processing": {
                "pyworld_available": pyworld_available,
                "supported_formats": list(self.tsubaki_processor.SUPPORTED_FORMATS.keys()),
                "f0_backends": get_f0_backend_status(),
            },
            "alt_aligners": get_alt_aligner_status(),   # ← 新增
        }
