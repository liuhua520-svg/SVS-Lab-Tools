# -*- coding: utf-8 -*-
"""
TTS 文本转语音模块（"讲述人" / "TTS跟读" 功能后端）

设计说明
────────
本模块独立于 MFA / WhisperX / Qwen3-ASR / NeMo-FA 等既有对齐后端，只负责：
  1. 讲述人档案（名字 + 引擎 + 音色 + 语速/音调/音量）的增删改查（JSON 落盘）；
  2. 一个可扩展的 TTS 引擎注册表（_ENGINES），当前内置两个引擎：
       - "edge_tts"     ：微软 EdgeTTS 在线合成（原有实现）；
       - "windows_sapi" ：调用 Windows 系统自带的语音合成功能（即"讲述人"，
                          通过 SAPI5 / pywin32 驱动，纯本地、不需要联网）；
     后续新增引擎只需要在 _ENGINES 里注册一个新的适配条目即可，不需要改动
     synthesize_and_align() / synthesize_preview() 等上层流程。
  3. 把整段输入文本按句末标点切分成句子，逐句合成 → 逐句对齐 → 按各句在
     合并音频中的时间偏移量整体平移该句 LAB 时间戳 → 拼接成完整 LAB。

为什么"逐句合成 + 逐句对齐"而不是"整段合成 + 整段对齐"：
  TTS 合成的整段音频里，句子之间的停顿时长完全由 TTS 引擎自行决定，与参考
  文本的书写顺序能保证严格对应；但一次性把很长的合成音频丢给强制对齐模型，
  容易在长音频上出现累积误差（Qwen3-FA 内部就是按窗口分块处理长音频，参见
  alt_aligners.py 里 Qwen3ForcedAligner._align_chunked 的说明）。这里反其道
  而行：每句话单独合成为一段"物理独立"的短音频，分别对齐，在完全已知的时间
  偏移量下拼接回去——每一句的对齐都在自己的短时序空间里完成，不存在"越往后
  越跑偏"的问题。

为什么固定使用 Qwen3-ForcedAligner（Qwen3-FA）：
  Qwen3-FA 是唯一专为"短音频、需要参考文本"的强制对齐场景设计、且已内置
  全局事后偏移校正（_apply_qwen3_fa_onset_delay）的后端，逐句合成产生的
  短句音频正好落在它最擅长的输入规模内；MFA 需要预装语言模型、WhisperX/
  Qwen3-ASR 主要面向"文本可选"的真人录音场景，都不如 Qwen3-FA 适合这里。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
NARRATORS_PATH = PROJECT_DIR / "settings" / "tts_narrators.json"
_narrator_lock = threading.RLock()

IS_WINDOWS = sys.platform.startswith("win")

# 合并音频统一采样率（Hz）。不同引擎原始输出的采样率/格式不一样（EdgeTTS
# 多为 24kHz mp3，Windows SAPI 这里固定按 44.1kHz/16bit/单声道 wav 落盘），
# 这里统一重采样到 44.1kHz，与工程文件生成链路（tsubaki_processor /
# f0_extractors）常见的音频采样率习惯保持一致。
SAMPLE_RATE = 44100

# 句间静音间隔（秒）：逐句合成时天然就有句尾静音，这里在物理拼接音频时额外
# 补一段更明显的停顿，便于 _fill_silences_lab() 识别为句间 SIL，也让最终
# 合成音频的语感更自然（避免上一句尾音直接贴上下一句开头）。
DEFAULT_SENTENCE_GAP_SEC = 0.35

# 句末标点分段：中/英/日/韩常见句末停顿符号 + 换行。
# 与 alt_aligners._SENTENCE_END_RE（r'[。！？；\n…!?]+'）的区别：这里额外
# 收录英文句点 "."——因为本模块每一句都会变成一次独立的 TTS 合成 + 独立
# 对齐调用，不存在"句子中间的小数点/缩写点"需要被保留在同一句内的顾虑，
# 用户需求也明确写的是"按句号分段"。
_SENTENCE_SPLIT_RE = re.compile(r'([。！？；.!?…\n]+)')

# 讲述人默认音色（按内部语言短代码），仅用于新建讲述人时给一个合理默认值，
# 用户在前端仍可自由更换为 list_voices() 返回的任意其它音色。
DEFAULT_VOICE_BY_LANG: Dict[str, str] = {
    "zh": "zh-CN-XiaoxiaoNeural", "cmn": "zh-CN-XiaoxiaoNeural",
    "yue": "zh-HK-HiuMaanNeural",
    "en": "en-US-AriaNeural", "eng": "en-US-AriaNeural",
    "ja": "ja-JP-NanamiNeural", "jpn": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural", "kor": "ko-KR-SunHiNeural",
}

# language 短代码 → EdgeTTS locale 语种大类前缀，用于 list_voices() 过滤。
_LOCALE_LANG_PREFIX: Dict[str, str] = {
    "zh": "zh", "cmn": "zh",
    "en": "en", "eng": "en",
    "ja": "ja", "jpn": "ja",
    "ko": "ko", "kor": "ko",
}
# 粤语单独按更精确的地区前缀过滤（zh-HK），不与普通话音色混在一起。
_LOCALE_EXACT_PREFIX: Dict[str, str] = {
    "yue": "zh-hk",
}


# ═════════════════════════════════════════════════════════════════════════
# 引擎注册表："选择 TTS" 下拉框显示的选项，及每个引擎的能力入口
#   engines 的 key 即前端 / API 传递的 engine 字符串（如 "edge_tts"）；
#   新增引擎：在下面追加一个 check_available 函数，并在 _ENGINES 里注册一
#   行，不需要改动 synthesize_and_align 等上层逻辑。
# ═════════════════════════════════════════════════════════════════════════

DEFAULT_ENGINE = "edge_tts"


def _edge_tts_check_available() -> Tuple[bool, str]:
    try:
        import edge_tts  # noqa: F401
        return True, "EdgeTTS 已就绪"
    except ImportError as e:
        return False, f"未安装 edge-tts: pip install -U edge-tts ({e})"


def _windows_sapi_check_available() -> Tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "讲述人（Windows 语音合成）仅支持 Windows 系统"
    try:
        import win32com.client  # noqa: F401
        return True, "讲述人（Windows SAPI）已就绪"
    except ImportError as e:
        return False, f"未安装 pywin32，无法使用讲述人: pip install pywin32 ({e})"


# 引擎注册表。label_zh 供前端"选择 TTS"下拉框直接展示，不需要再单独维护
# i18n key——新增引擎只需要在这里追加一行即可在前端出现。
_ENGINES: Dict[str, Dict] = {
    "windows_sapi": {
        "id": "windows_sapi",
        "label": "Narrator (Windows)",
        "label_zh": "讲述人",
        "check_available": _windows_sapi_check_available,
    },
    "edge_tts": {
        "id": "edge_tts",
        "label": "EdgeTTS",
        "label_zh": "EdgeTTS",
        "check_available": _edge_tts_check_available,
    },
    # 后续新增引擎（如某个本地 TTS 模型）在此追加一行即可，前端"选择 TTS"
    # 下拉框会自动出现新选项，不需要改动 Vue 组件。
}


def list_engines() -> List[Dict]:
    """
    返回全部已注册 TTS 引擎及其可用性，供前端渲染"选择 TTS"下拉框。
    """
    out = []
    for engine_id, meta in _ENGINES.items():
        ok, msg = meta["check_available"]()
        out.append({
            "id": engine_id,
            "label": meta["label"],
            "label_zh": meta["label_zh"],
            "available": ok,
            "message": msg,
        })
    return out


def check_available(engine: Optional[str] = None) -> Tuple[bool, str]:
    engine = engine or DEFAULT_ENGINE
    meta = _ENGINES.get(engine)
    if not meta:
        return False, f"未知 TTS 引擎: {engine}"
    return meta["check_available"]()


# ═════════════════════════════════════════════════════════════════════════
# 讲述人档案：JSON 落盘的简单 CRUD（与 app_settings.py 的存储方式保持一致）
#   注意："讲述人档案"（narrator profile）是"语音预设"的概念——即预先保存
#   好的一组 {引擎 + 音色 + 语速/音调/音量} 命名快捷方式，与"选择 TTS"引擎
#   下拉框（决定用 EdgeTTS 还是 Windows 讲述人本身）是两个不同层面的东西：
#   预设可以基于任意一个已注册引擎创建。
# ═════════════════════════════════════════════════════════════════════════

def _load_narrators() -> List[Dict]:
    with _narrator_lock:
        if not NARRATORS_PATH.exists():
            return []
        try:
            data = json.loads(NARRATORS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"讲述人配置读取失败，返回空列表: {e}")
            return []


def _save_narrators(items: List[Dict]) -> None:
    with _narrator_lock:
        NARRATORS_PATH.parent.mkdir(parents=True, exist_ok=True)
        NARRATORS_PATH.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def list_narrators() -> List[Dict]:
    """返回全部讲述人档案（语音预设），按名称排序。"""
    items = _load_narrators()
    # 兼容旧数据：早期版本没有 engine 字段（当时只有 EdgeTTS 一个引擎），
    # 读取时统一补齐为 DEFAULT_ENGINE，避免前端按引擎过滤预设列表时把老
    # 预设"过滤没了"。
    for it in items:
        it.setdefault("engine", DEFAULT_ENGINE)
    return sorted(items, key=lambda x: (x.get("name") or ""))


def upsert_narrator(profile: Dict) -> Dict:
    """
    新建或更新一个讲述人档案（语音预设）。

    profile: {id?, name, engine?, voice, rate, pitch, volume, language}
      id 为空 / 未提供 → 新建（生成随机 id）；
      id 命中已有档案 → 覆盖更新；否则忽略传入的 id，视为新建。
      engine 未提供时默认为 DEFAULT_ENGINE（向后兼容旧前端/旧数据）。
    """
    items = _load_narrators()
    narrator_id = (profile.get("id") or "").strip()
    if not narrator_id or not any(it.get("id") == narrator_id for it in items):
        narrator_id = uuid.uuid4().hex[:12]

    engine = (profile.get("engine") or "").strip() or DEFAULT_ENGINE
    if engine not in _ENGINES:
        engine = DEFAULT_ENGINE

    record = {
        "id": narrator_id,
        "name": (profile.get("name") or "").strip() or "讲述人",
        "engine": engine,
        "voice": (profile.get("voice") or "").strip(),
        "rate": _normalize_percent(profile.get("rate")),
        "pitch": _normalize_pitch(profile.get("pitch")),
        "volume": _normalize_percent(profile.get("volume")),
        "language": (profile.get("language") or "").strip(),
    }

    replaced = False
    for i, it in enumerate(items):
        if it.get("id") == narrator_id:
            items[i] = record
            replaced = True
            break
    if not replaced:
        items.append(record)

    _save_narrators(items)
    return record


def delete_narrator(narrator_id: str) -> bool:
    items = _load_narrators()
    new_items = [it for it in items if it.get("id") != narrator_id]
    changed = len(new_items) != len(items)
    if changed:
        _save_narrators(new_items)
    return changed


# ═════════════════════════════════════════════════════════════════════════
# 语速 / 音调 / 音量 参数归一化
#   前端统一按 EdgeTTS 的约定发送：
#     rate / volume: "+N%" / "-N%"（相对百分比）
#     pitch        : "+NHz" / "-NHz"（相对赫兹）
#   —— 两个引擎共用同一套前端滑块和取值约定，各引擎自己的适配层负责把这
#   套通用值再换算成自己的原生参数范围（见下面 _sapi_* 系列函数）。
#   前端可能直接送已经格式化好的字符串，也可能送纯数字（滑块场景）——
#   这里统一兜底成通用字符串格式，避免因格式不对导致合成报错。
# ═════════════════════════════════════════════════════════════════════════

def _normalize_percent(value, default: str = "+0%") -> str:
    if value is None or value == "":
        return default
    s = str(value).strip()
    if s.endswith("%"):
        return s if (s.startswith("+") or s.startswith("-")) else f"+{s}"
    try:
        n = float(s)
        return f"{'+' if n >= 0 else ''}{int(n)}%"
    except ValueError:
        return default


def _normalize_pitch(value, default: str = "+0Hz") -> str:
    if value is None or value == "":
        return default
    s = str(value).strip()
    if s.lower().endswith("hz"):
        return s if (s.startswith("+") or s.startswith("-")) else f"+{s}"
    try:
        n = float(s)
        return f"{'+' if n >= 0 else ''}{int(n)}Hz"
    except ValueError:
        return default


def _percent_to_number(value: str) -> float:
    try:
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return 0.0


def _hz_to_number(value: str) -> float:
    try:
        return float(str(value).strip().lower().rstrip("hz"))
    except ValueError:
        return 0.0


# ═════════════════════════════════════════════════════════════════════════
# 音色列表（按引擎分发）
# ═════════════════════════════════════════════════════════════════════════

def list_voices(engine: Optional[str] = None, language: Optional[str] = None) -> List[Dict]:
    """
    返回指定引擎可用的音色列表（[{id, name, gender, locale}, ...]）。

    engine 为 None 时默认 DEFAULT_ENGINE（向后兼容旧前端只传 language 的
    调用方式）。language 为内部语言短代码（zh/cmn/en/eng/ja/jpn/ko/kor/yue
    等，与前端"语言"下拉一致）时，只返回该语种大类对应的音色；传 None /
    未识别的语种代码时返回全部音色，交给前端自行搜索。
    """
    engine = engine or DEFAULT_ENGINE
    if engine == "windows_sapi":
        return _sapi_list_voices(language)
    return _edge_tts_list_voices(language)


def _edge_tts_list_voices(language: Optional[str] = None) -> List[Dict]:
    import edge_tts

    async def _fetch():
        return await edge_tts.list_voices()

    raw = asyncio.run(_fetch())

    lang_key = (language or "").lower()
    exact_prefix = _LOCALE_EXACT_PREFIX.get(lang_key)
    loose_prefix = _LOCALE_LANG_PREFIX.get(lang_key)

    out: List[Dict] = []
    for v in raw:
        locale = v.get("Locale", "")
        locale_lower = locale.lower()
        if exact_prefix and not locale_lower.startswith(exact_prefix):
            continue
        if not exact_prefix and loose_prefix and not locale_lower.startswith(loose_prefix):
            continue
        out.append({
            "id": v.get("ShortName"),
            "name": v.get("FriendlyName") or v.get("ShortName"),
            "gender": v.get("Gender"),
            "locale": locale,
        })

    out.sort(key=lambda x: (x["locale"], x["name"]))
    return out


def _sapi_list_voices(language: Optional[str] = None) -> List[Dict]:
    """
    枚举 Windows 系统已安装的 SAPI5 语音（即"讲述人"设置里能看到的那些）。

    讲述人音色数量通常很少（系统自带 1~3 个，如 David/Zira/Huihui 等），
    这里不按 language 严格过滤——很多系统自带语音的 Attributes 里并不总是
    携带规范的语言代码，严格过滤反而可能把用户唯一安装的音色误筛掉；语言
    信息仍然会尽量读出来展示在 locale 字段里，供用户自行辨认。
    """
    ok, msg = _windows_sapi_check_available()
    if not ok:
        raise RuntimeError(msg)

    import win32com.client
    speaker = win32com.client.Dispatch("SAPI.SpVoice")
    tokens = speaker.GetVoices()

    out: List[Dict] = []
    for i in range(tokens.Count):
        token = tokens.Item(i)
        try:
            name = token.GetDescription()
        except Exception:
            name = getattr(token, "Id", f"voice_{i}")
        try:
            locale = token.GetAttribute("Language")
        except Exception:
            locale = ""
        try:
            gender = token.GetAttribute("Gender")
        except Exception:
            gender = ""
        out.append({
            "id": token.Id,
            "name": name,
            "gender": gender,
            "locale": locale,
        })
    return out


# ═════════════════════════════════════════════════════════════════════════
# Windows SAPI（"讲述人"）合成实现
#   通过 pywin32 直接驱动 SAPI.SpVoice + SAPI.SpFileStream，不依赖
#   gencache/makepy 预生成的类型库（用固定的数值常量代替
#   win32com.client.constants），这样在 PyInstaller 打包环境下也能稳定
#   工作，不会因为运行时缺少 gen_py 缓存而报错。
# ═════════════════════════════════════════════════════════════════════════

# SpeechStreamFileMode.SSFMCreateForWrite
_SSFM_CREATE_FOR_WRITE = 3
# SpeechAudioFormatType.SAFT44kHz16BitMono
_SAFT_44K_16BIT_MONO = 34
# SpeechVoiceSpeakFlags.SVSFIsXML（讲述人音调通过内联 SAPI XML <pitch> 标签
# 实现时，需要用这个 flag 告诉 SAPI 把文本当 XML 解析）
_SVSF_IS_XML = 8


def _sapi_percent_to_rate(rate: str) -> int:
    """通用 "+N%"/"-N%" → SAPI Rate（-10..10，默认 0）。"""
    n = _percent_to_number(rate)
    return max(-10, min(10, int(round(n / 10))))


def _sapi_percent_to_volume(volume: str) -> int:
    """通用 "+N%"/"-N%" → SAPI Volume（0..100，默认 100）。"""
    n = _percent_to_number(volume)
    return max(0, min(100, int(round(100 + n))))


def _sapi_hz_to_pitch_level(pitch: str) -> int:
    """
    通用 "+NHz"/"-NHz" → SAPI 内联 XML <pitch absmiddle="X"> 的 X（-10..10）。

    SAPI 原生不像 EdgeTTS 一样支持以 Hz 为单位连续调节音调，这里按每 5Hz
    约等于 1 档做粗略换算，属于尽力而为的近似值；部分较新的语音（尤其是
    非经典 Desktop 语音）可能不响应该 XML 标签，此时音调调整会被忽略但
    不影响正常合成。
    """
    n = _hz_to_number(pitch)
    return max(-10, min(10, int(round(n / 5))))


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _sapi_find_voice_token(speaker, voice_id: str):
    if not voice_id:
        return None
    tokens = speaker.GetVoices()
    for i in range(tokens.Count):
        token = tokens.Item(i)
        if token.Id == voice_id:
            return token
    return None


def _sapi_synth_to_file(text: str, voice_id: str, rate: str, volume: str,
                         pitch: str, out_path: str) -> None:
    """
    调用 Windows SAPI5 把 text 合成为 wav 文件，直接落盘到 out_path（固定
    44.1kHz / 16bit / 单声道，与 SAMPLE_RATE 保持一致，后续管线不需要再
    对讲述人音频单独做重采样判断）。
    """
    ok, msg = _windows_sapi_check_available()
    if not ok:
        raise RuntimeError(msg)

    import win32com.client

    speaker = win32com.client.Dispatch("SAPI.SpVoice")
    token = _sapi_find_voice_token(speaker, voice_id)
    if token is not None:
        speaker.Voice = token
    # 否则使用 SAPI 当前默认音色（不因为 voice_id 为空/未匹配而直接报错，
    # 保证至少能听到默认讲述人的声音）。

    speaker.Rate = _sapi_percent_to_rate(rate)
    speaker.Volume = _sapi_percent_to_volume(volume)

    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Format.Type = _SAFT_44K_16BIT_MONO
    stream.Open(out_path, _SSFM_CREATE_FOR_WRITE, False)

    prev_output = speaker.AudioOutputStream
    speaker.AudioOutputStream = stream
    try:
        pitch_level = _sapi_hz_to_pitch_level(pitch)
        if pitch_level != 0:
            xml_text = f'<pitch absmiddle="{pitch_level}">{_xml_escape(text)}</pitch>'
            speaker.Speak(xml_text, _SVSF_IS_XML)
        else:
            speaker.Speak(text, 0)
    finally:
        speaker.AudioOutputStream = prev_output
        stream.Close()


# ═════════════════════════════════════════════════════════════════════════
# 句子切分
# ═════════════════════════════════════════════════════════════════════════

def split_sentences(text: str) -> List[str]:
    """把整段文本按句末标点切成句子列表（标点保留在句尾），过滤空句。"""
    if not text or not text.strip():
        return []

    parts = _SENTENCE_SPLIT_RE.split(text)
    sentences: List[str] = []
    buf = ""
    for part in parts:
        if not part:
            continue
        buf += part
        if _SENTENCE_SPLIT_RE.fullmatch(part):
            if buf.strip():
                sentences.append(buf.strip())
            buf = ""
    if buf.strip():
        sentences.append(buf.strip())

    return sentences


# ═════════════════════════════════════════════════════════════════════════
# EdgeTTS 单句合成
# ═════════════════════════════════════════════════════════════════════════

async def _edge_tts_synth_to_file_async(text: str, voice: str, rate: str, volume: str,
                                         pitch: str, out_path: str) -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate,
                                        volume=volume, pitch=pitch)
    await communicate.save(out_path)


def _edge_tts_synth_to_file(text: str, voice: str, rate: str, volume: str,
                             pitch: str, out_path: str) -> None:
    asyncio.run(_edge_tts_synth_to_file_async(text, voice, rate, volume, pitch, out_path))


# ═════════════════════════════════════════════════════════════════════════
# 单句合成分发器 + 试听预览（按引擎选择实现，向上层完全屏蔽差异）
# ═════════════════════════════════════════════════════════════════════════

def segment_file_suffix(engine: str) -> str:
    """该引擎单句合成产物的原生文件后缀（EdgeTTS 输出 mp3，讲述人直接输出 wav）。"""
    return ".wav" if engine == "windows_sapi" else ".mp3"


def synthesize_segment_to_file(text: str, voice: str, rate: str, volume: str,
                                pitch: str, out_path: str,
                                engine: str = DEFAULT_ENGINE) -> None:
    """同步合成一段文本到 out_path（文件后缀需与 engine 匹配，参见 segment_file_suffix）。"""
    if engine == "windows_sapi":
        _sapi_synth_to_file(text, voice, rate, volume, pitch, out_path)
    else:
        _edge_tts_synth_to_file(text, voice, rate, volume, pitch, out_path)


def synthesize_preview(text: str, voice: str, rate: str = "+0%",
                        volume: str = "+0%", pitch: str = "+0Hz",
                        engine: str = DEFAULT_ENGINE) -> bytes:
    """
    快速试听：只做一次单句合成（不切句、不对齐），返回音频字节数据（EdgeTTS
    为 mp3，讲述人为 wav），供前端 <audio> 直接播放。预览文本过长时截断到
    前 200 字——预览的目的是试听音色/语速/音调，不需要整段文本，避免不必要
    的等待。
    """
    ok, msg = check_available(engine)
    if not ok:
        raise RuntimeError(msg)

    text = (text or "").strip()
    if not text:
        raise ValueError("预览文本为空")
    if not voice:
        raise ValueError("未指定音色")

    preview_text = text[:200]
    suffix = segment_file_suffix(engine)
    tmp_path = Path(tempfile.gettempdir()) / f"tts_preview_{uuid.uuid4().hex[:8]}{suffix}"
    try:
        synthesize_segment_to_file(
            preview_text, voice,
            _normalize_percent(rate), _normalize_percent(volume), _normalize_pitch(pitch),
            str(tmp_path), engine=engine,
        )
        return tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)


# ═════════════════════════════════════════════════════════════════════════
# LAB 条目（100ns 单位，与 alt_aligners.py 全文保持一致）解析 / 平移 / 拼接
# ═════════════════════════════════════════════════════════════════════════

def _parse_lab_lines(lab_content: str) -> List[Tuple[int, int, str]]:
    entries: List[Tuple[int, int, str]] = []
    for line in (lab_content or "").strip().splitlines():
        parts = line.strip().split()
        if len(parts) >= 3:
            try:
                entries.append((int(parts[0]), int(parts[1]), parts[2]))
            except ValueError:
                continue
    return entries


def _shift_entries(entries: List[Tuple[int, int, str]], offset_100ns: int) -> List[Tuple[int, int, str]]:
    return [(s + offset_100ns, e + offset_100ns, p) for s, e, p in entries]


def _entries_to_lab_text(entries: List[Tuple[int, int, str]]) -> str:
    return "\n".join(f"{s} {e} {p}" for s, e, p in entries)


def _uniform_fallback_entries(text: str, duration_100ns: int) -> List[Tuple[int, int, str]]:
    """
    单句对齐失败时的兜底：把该句非空白字符在该句合成时长内均匀分配时间戳，
    保证整个批处理不会因为个别句子的对齐异常而整体中断。属于极少数场景
    下的安全网，不是正常路径（正常情况下 Qwen3-FA 短句对齐成功率很高）。
    """
    chars = [ch for ch in text if not ch.isspace()]
    n = len(chars) or 1
    step = duration_100ns / n
    entries: List[Tuple[int, int, str]] = []
    for i, ch in enumerate(chars):
        s = int(round(i * step))
        e = int(round((i + 1) * step))
        entries.append((s, e, ch))
    return entries


def _get_wav_duration_100ns(path: str) -> int:
    import soundfile as sf
    data, sr = sf.read(path)
    frames = len(data)
    return int(round((frames / sr) * 10_000_000))


def get_wav_duration_100ns(path: str) -> int:
    """公开版本的 _get_wav_duration_100ns，供 app.py 在复用预览音频、
    重建最终结果时计算 audio_duration，不需要越权调用下划线私有函数。"""
    return _get_wav_duration_100ns(path)


def _load_audio_mono(path: str, sr: int = SAMPLE_RATE):
    """读取任意单句合成输出（mp3/wav 均可）为单声道 float32 数组，统一采样率。"""
    import librosa
    y, _ = librosa.load(path, sr=sr, mono=True)
    return y


# ═════════════════════════════════════════════════════════════════════════
# 主流程：整段文本 → 逐句合成 → 逐句对齐 → 合并 WAV + LAB
# ═════════════════════════════════════════════════════════════════════════

def synthesize_segments_only(
    text: str,
    language: str,
    voice: str,
    work_dir: str,
    stem: str,
    engine: str = DEFAULT_ENGINE,
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
    sentence_gap_sec: float = DEFAULT_SENTENCE_GAP_SEC,
    sentences: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Dict:
    """
    仅执行"逐句 TTS 合成"这一半流程，不做 Qwen3-FA 对齐：

      1. 按句末标点把 text 切分成句子列表（sentences 非空时直接复用调用方
         传入的句子列表，跳过内部切分）；
      2. 逐句调用所选 engine 合成为独立的物理音频片段，统一转换成
         16-bit PCM WAV 后保留在 segments_dir 下（seg_0000.wav,
         seg_0001.wav, ... 与返回的 sentences 列表按下标一一对应），供
         align_segments() 之后复用——这是"预览"和"开始处理"之间不用把
         同一段文本合成两遍的关键：预览阶段先调这个函数拿到分句音频，
         用户点"开始处理"时只需再跑 align_segments()；如果用户没有先
         手动预览，则由 synthesize_and_align() 把这两步接起来一次做完；
      3. 把保留下来的分句音频依次拼接（句间插入 sentence_gap_sec 静音）
         成一份完整 WAV，供前端 <audio> 直接播放试听，也作为最终产物的
         音频文件（对齐阶段不会重新合成或改动这份音频）。

    Returns
    -------
    Dict:
      成功: {
        "success": True,
        "wav_path": str,          # 拼接后的完整音频（预览播放 / 最终产物共用）
        "segments_dir": str,      # 分句音频所在目录，align_segments() 需要
        "sentences": [str, ...],  # 实际合成成功的句子列表，与 segments_dir 一一对应
        "sentence_count": int,
        "audio_duration": int,    # 100ns 单位
        "warnings": [str, ...],   # 个别分句合成失败时的提示（已跳过，不计入 sentences）
      }
      失败: {"success": False, "error": str}
    """
    engine = engine or DEFAULT_ENGINE
    ok, msg = check_available(engine)
    if not ok:
        return {"success": False, "error": msg}

    text = (text or "").strip()
    if not text:
        return {"success": False, "error": "文本为空，无法生成语音"}
    if not voice:
        return {"success": False, "error": "未指定音色"}

    sentence_list = sentences if sentences is not None else split_sentences(text)
    if not sentence_list:
        return {"success": False, "error": "未能从输入文本中切分出任何句子"}

    rate = _normalize_percent(rate)
    volume = _normalize_percent(volume)
    pitch = _normalize_pitch(pitch)

    import numpy as np
    import soundfile as sf

    work_dir_path = Path(work_dir)
    work_dir_path.mkdir(parents=True, exist_ok=True)
    segments_dir = work_dir_path / f"_tts_segments_{stem}"
    segments_dir.mkdir(parents=True, exist_ok=True)

    seg_suffix = segment_file_suffix(engine)
    total = len(sentence_list)
    audio_chunks: List["np.ndarray"] = []
    warnings: List[str] = []
    kept_sentences: List[str] = []
    gap_samples = np.zeros(max(0, int(round(sentence_gap_sec * SAMPLE_RATE))), dtype="float32")

    try:
        for i, sentence in enumerate(sentence_list):
            seg_raw = segments_dir / f"_raw_{i:04d}{seg_suffix}"
            try:
                synthesize_segment_to_file(sentence, voice, rate, volume, pitch,
                                            str(seg_raw), engine=engine)
            except Exception as e:
                logger.error(f"[TTS] 第 {i + 1}/{total} 句合成失败: {e}", exc_info=True)
                warnings.append(f"第 {i + 1} 句合成失败已跳过：{e}")
                if progress_cb:
                    progress_cb(i + 1, total)
                continue

            seg_samples = _load_audio_mono(str(seg_raw), sr=SAMPLE_RATE)
            seg_raw.unlink(missing_ok=True)

            # 用"已保留成功句子的当前数量"命名，保证 seg_XXXX.wav 与
            # kept_sentences 的下标严格连续对应（即使中途有句子合成失败被
            # 跳过，也不会在 align_segments() 里对错文件）。
            seg_wav = segments_dir / f"seg_{len(kept_sentences):04d}.wav"
            sf.write(str(seg_wav), seg_samples, SAMPLE_RATE, subtype="PCM_16")

            if audio_chunks:
                audio_chunks.append(gap_samples)
            audio_chunks.append(seg_samples)
            kept_sentences.append(sentence)

            if progress_cb:
                progress_cb(i + 1, total)

        if not kept_sentences:
            shutil.rmtree(str(segments_dir), ignore_errors=True)
            return {"success": False, "error": "全部分句合成失败，无法生成音频"}

        merged = np.concatenate(audio_chunks) if len(audio_chunks) > 1 else audio_chunks[0]
        wav_path = str(work_dir_path / f"{stem}.wav")
        sf.write(wav_path, merged, SAMPLE_RATE, subtype="PCM_16")

        return {
            "success": True,
            "wav_path": wav_path,
            "segments_dir": str(segments_dir),
            "sentences": kept_sentences,
            "sentence_count": len(kept_sentences),
            "audio_duration": _get_wav_duration_100ns(wav_path),
            "warnings": warnings,
        }
    except Exception as e:
        shutil.rmtree(str(segments_dir), ignore_errors=True)
        logger.error(f"[TTS] 分句合成失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def align_segments(
    segments_dir: str,
    sentences: List[str],
    language: str,
    aligner_device: Optional[str] = None,
    english_word_align: bool = False,
    sentence_gap_sec: float = DEFAULT_SENTENCE_GAP_SEC,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Dict:
    """
    TTS 跟读的对齐半流程：对 synthesize_segments_only() 已经产出的分句
    音频（segments_dir 下的 seg_0000.wav, seg_0001.wav, ... 与 sentences
    按下标一一对应）逐句调用 Qwen3-ForcedAligner 对齐，按各句在合并音频
    中的偏移量整体平移该句 LAB 时间戳，最终拼接统一跑一次
    alt_aligners._fill_silences_lab() 转换出开头 / 句间静音的 SIL 条目。

    不会重新调用 TTS 引擎合成——这是"先点预览生成分句音频，再点开始处理
    只需对齐"这条路径的核心，避免同一段文本被合成两遍。

    Returns
    -------
    Dict:
      成功: {"success": True, "lab_content": str, "sentence_count": int, "warnings": [str, ...]}
      失败: {"success": False, "error": str}
    """
    from alt_aligners import get_aligner, _fill_silences_lab

    segments_path = Path(segments_dir)
    total = len(sentences)
    if total == 0:
        return {"success": False, "error": "没有可对齐的分句音频"}

    try:
        aligner = get_aligner("qwen3_aligner", device=(aligner_device or "auto"))
    except Exception as e:
        return {"success": False, "error": f"Qwen3-ForcedAligner 初始化失败: {e}"}

    lab_entries: List[Tuple[int, int, str]] = []
    warnings: List[str] = []
    cumulative_100ns = 0
    gap_100ns = int(round(sentence_gap_sec * 10_000_000))

    for i, sentence in enumerate(sentences):
        seg_wav = segments_path / f"seg_{i:04d}.wav"
        if not seg_wav.exists():
            warnings.append(f"第 {i + 1} 句音频缺失，已跳过")
            if progress_cb:
                progress_cb(i + 1, total)
            continue

        seg_duration_100ns = _get_wav_duration_100ns(str(seg_wav))

        try:
            align_result = aligner.align(str(seg_wav), sentence, language,
                                          english_word_align=english_word_align)
        except Exception as e:
            align_result = {"success": False, "error": str(e)}

        if align_result.get("success"):
            local_entries = _parse_lab_lines(align_result.get("lab_content", ""))
        else:
            logger.warning(
                f"[TTS] 第 {i + 1}/{total} 句 Qwen3-FA 对齐失败，"
                f"使用均匀分配兜底：{align_result.get('error')}"
            )
            warnings.append(f"第 {i + 1} 句对齐失败，已使用均匀时间戳兜底：{align_result.get('error')}")
            local_entries = _uniform_fallback_entries(sentence, seg_duration_100ns)

        lab_entries.extend(_shift_entries(local_entries, cumulative_100ns))
        cumulative_100ns += seg_duration_100ns
        if i < total - 1:
            cumulative_100ns += gap_100ns

        if progress_cb:
            progress_cb(i + 1, total)

    lab_entries.sort(key=lambda t: t[0])
    raw_lab_text = _entries_to_lab_text(lab_entries)
    final_lab_text = _fill_silences_lab(raw_lab_text) if raw_lab_text else ""

    return {
        "success": True,
        "lab_content": final_lab_text,
        "sentence_count": total,
        "warnings": warnings,
    }


def synthesize_and_align(
    text: str,
    language: str,
    voice: str,
    work_dir: str,
    stem: str,
    engine: str = DEFAULT_ENGINE,
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
    aligner_device: Optional[str] = None,
    english_word_align: bool = False,
    sentence_gap_sec: float = DEFAULT_SENTENCE_GAP_SEC,
    sentences: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Dict:
    """
    TTS 跟读主流程（合成 + 对齐一次性做完）：依次调用
    synthesize_segments_only() 和 align_segments()。

    等价于"没有先手动点预览，直接点开始处理"这条路径——先合成分句音频，
    再整体交给 Qwen3-FA 对齐。如果调用方已经通过
    synthesize_segments_only()（例如 /api/tts/synthesize_preview）拿到了
    分句音频，应该直接调用 align_segments() 复用，不要再调这个函数从头
    合成一遍。

    Returns
    -------
    Dict:
      成功: {
        "success": True,
        "wav_path": str, "lab_path": str, "lab_content": str,
        "audio_duration": int,      # 100ns 单位，与其它对齐后端返回字段一致
        "sentence_count": int,
        "warnings": [str, ...],      # 个别分句合成/对齐失败时的兜底提示
      }
      失败: {"success": False, "error": str}
    """
    seg_result = synthesize_segments_only(
        text=text, language=language, voice=voice, work_dir=work_dir, stem=stem,
        engine=engine, rate=rate, volume=volume, pitch=pitch,
        sentence_gap_sec=sentence_gap_sec, sentences=sentences, progress_cb=progress_cb,
    )
    if not seg_result.get("success"):
        return seg_result

    segments_dir = seg_result["segments_dir"]
    try:
        align_result = align_segments(
            segments_dir=segments_dir, sentences=seg_result["sentences"], language=language,
            aligner_device=aligner_device, english_word_align=english_word_align,
            sentence_gap_sec=sentence_gap_sec, progress_cb=progress_cb,
        )
    finally:
        shutil.rmtree(str(segments_dir), ignore_errors=True)

    if not align_result.get("success"):
        return align_result

    lab_path = str(Path(work_dir) / f"{stem}.lab")
    Path(lab_path).write_text(align_result["lab_content"], encoding="utf-8")

    return {
        "success": True,
        "wav_path": seg_result["wav_path"],
        "lab_path": lab_path,
        "lab_content": align_result["lab_content"],
        "audio_duration": seg_result["audio_duration"],
        "sentence_count": align_result["sentence_count"],
        "warnings": seg_result["warnings"] + align_result["warnings"],
    }
