# -*- coding: utf-8 -*-
# ── 警告过滤：必须在所有其他 import 之前执行 ─────────────────────────────────────
# pyannote.audio.core.io 在 torchcodec DLL 缺失时用 warnings.warn() 发出 UserWarning。
# 该警告消息以 "\n" 开头，因此 re.match(r".*torchcodec", msg) 匹配失败。
# 正确做法：按 module 名过滤，完全绕开消息内容匹配。
import warnings
warnings.filterwarnings("ignore", module=r"pyannote\.audio\.core\.io")
warnings.filterwarnings("ignore", module=r"pyannote\.audio")
warnings.filterwarnings("ignore", module=r"speechbrain\.utils\.torch_audio_backend")
warnings.filterwarnings("ignore", module=r"speechbrain\.utils\.quirks")
# ─────────────────────────────────────────────────────────────────────────────
import os
import re
import shutil
import sys
import uuid
import json
import base64
import logging
import webbrowser
from urllib.parse import quote
from threading import Thread
from time import sleep
from pathlib import Path
from typing import Dict, Optional

from flask import Flask, request, jsonify, send_from_directory, abort, Response
from flask_cors import CORS
import requests

from mfa_utils import MFAChecker
from mfa_processor import MFAProcessor
from pipeline import AudioProcessingPipeline
from alt_aligners import get_alt_aligner_status
import dictionary_manager
import app_settings
import tts_processor
import text_processor
import subtitle_processor
import subtitle_import

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def _parse_align_pitch_shift(raw) -> float:
    """
    解析"对齐辅助移调"表单字段（半音，浮点数，正数升调/负数降调）。
    夹到 [-24, 24] 半音（两个八度）防止极端输入把 librosa.pitch_shift
    的相位声码器算法拖入明显失真区间；0（默认值）表示完全不启用，
    与该功能上线前的行为保持一致。
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return max(-24.0, min(24.0, v))


def _decode_subtitle_text(raw: bytes) -> str:
    """
    解码上传的 SRT/LRC 字幕文件字节流。多数字幕文件是 UTF-8（含/不含
    BOM），但仍有相当一部分老字幕（尤其国内翻译组产出的 SRT）是 GBK/GB18030
    编码，直接按 UTF-8 解码会抛异常或产出乱码——依次尝试 utf-8-sig →
    utf-8 → gb18030，都失败则用 utf-8 + errors="replace" 兜底，保证至少
    不会 500，只是极端情况下个别字符显示为替换符。
    """
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _normalize_dict_source(raw: str) -> str:
    """
    校验前端传来的 dict_source。

    词典来源现已改为"任意独立词典"（用户在词典管理页面自行创建、命名），
    不再是写死的 synthesizerv/vocaloid 两个选项，因此这里不能再用固定元组
    做大小写不敏感比较——词典名是大小写敏感的用户自定义字符串。
    只做两件事：
      1. "default"（大小写不敏感）→ 统一为 "default" 哨兵值；
      2. 其余值 → 若确实是当前存在的词典名则原样保留，否则安全回退为
         "default"（词典可能已被用户删除，静默回退比 500 报错更安全）。
    """
    value = (raw or "").strip()
    if not value or value.lower() == "default":
        return "default"
    try:
        if dictionary_manager.dictionary_exists(value):
            return value
    except Exception:
        pass
    return "default"


def _parse_box_override(form, index: int) -> Optional[Dict]:
    """
    解析对话文本框批量处理里第 index 个对话框的"单独设置"覆盖值。

    前端（DialogueBatch.vue 的"单独设置"弹窗）仅在该框开启了总开关
    （override_enabled_{i} == "true"）时才提交具体字段；未开启或字段
    完全缺失（如旧版前端）时返回 None，调用方应据此完全回退到整批
    统一的全局设置，与该功能上线前的行为保持一致。

    注意：不包含 bpm——BPM 决定整批对话框合并后的时间轴换算（每段的
    起始位置 = 前面所有对话框真实音频时长之和，换算成 blick/tick 时
    使用同一个 bpm），单独覆盖会破坏时间轴对齐，因此 BPM 恒定使用
    全局设置，不接受每框覆盖（前端弹窗里也没有提供该字段）。

    Returns
    -------
    Optional[Dict]：未开启覆盖时为 None；开启时返回校验/规范化后的字典，
        字段命名与 process_dialogue_batch 里单个 box 需要的覆盖键一致，
        便于调用方直接 dict.update / get 使用。
    """
    enabled = form.get(f"override_enabled_{index}", "false").lower() == "true"
    if not enabled:
        return None

    aligner_backend = form.get(f"override_aligner_backend_{index}", "mfa")
    if aligner_backend not in ("mfa", "whisperx", "qwen3_asr", "qwen3_aligner", "nemo_aligner"):
        aligner_backend = "mfa"

    language = form.get(f"override_language_{index}", "cmn")

    english_word_align = form.get(f"override_english_word_align_{index}", "false").lower() == "true"
    word_phoneme_map = form.get(f"override_word_phoneme_map_{index}", "false").lower() == "true"
    if word_phoneme_map and language != "jpn":
        # 与整批全局提交逻辑一致：开启单词映射音素时自动补上英语单词级
        # 对齐，确保对齐阶段产出整词级 LAB，词典查词才有意义。
        english_word_align = True

    phoneme_mode = form.get(f"override_phoneme_mode_{index}", "none")
    if phoneme_mode not in ("none", "merge", "hiragana", "katakana"):
        phoneme_mode = "none"

    dict_source = _normalize_dict_source(form.get(f"override_dict_source_{index}", "default"))

    try:
        base_pitch = int(form.get(f"override_base_pitch_{index}", 60))
    except (TypeError, ValueError):
        base_pitch = 60

    auto_note_pitch = form.get(f"override_auto_note_pitch_{index}", "true").lower() == "true"
    export_pitch_line = form.get(f"override_export_pitch_line_{index}", "true").lower() == "true"

    f0_method = form.get(f"override_f0_method_{index}", "dio")
    if f0_method not in ("dio", "harvest", "crepe", "rmvpe"):
        f0_method = "dio"

    f0_device = form.get(f"override_f0_device_{index}", "auto")
    if f0_device not in ("auto", "cpu", "cuda"):
        f0_device = "auto"

    crepe_model = form.get(f"override_crepe_model_{index}", "full")
    if crepe_model not in ("full", "tiny"):
        crepe_model = "full"

    precision = form.get(f"override_precision_{index}", "double")
    use_double_precision = precision.lower() == "double"

    f0_smooth = form.get(f"override_f0_smooth_{index}", "true").lower() == "true"

    try:
        f0_smooth_window = int(form.get(f"override_f0_smooth_window_{index}", 5))
    except (TypeError, ValueError):
        f0_smooth_window = 5

    try:
        vsqx_pitch_smooth_window = int(form.get(f"override_vsqx_pitch_smooth_window_{index}", 5))
    except (TypeError, ValueError):
        vsqx_pitch_smooth_window = 5

    try:
        f0_floor = float(form.get(f"override_f0_floor_{index}", 71.0))
    except (TypeError, ValueError):
        f0_floor = 71.0

    try:
        f0_ceil = float(form.get(f"override_f0_ceil_{index}", 800.0))
    except (TypeError, ValueError):
        f0_ceil = 800.0

    return {
        "aligner_backend": aligner_backend,
        "language": language,
        "english_word_align": english_word_align,
        "word_phoneme_map": word_phoneme_map,
        "phoneme_mode": phoneme_mode,
        "dict_source": dict_source,
        "base_pitch": base_pitch,
        "auto_note_pitch": auto_note_pitch,
        "export_pitch_line": export_pitch_line,
        "f0_method": f0_method,
        "f0_device": f0_device,
        "crepe_model": crepe_model,
        "use_double_precision": use_double_precision,
        "f0_smooth": f0_smooth,
        "f0_smooth_window": f0_smooth_window,
        "vsqx_pitch_smooth_window": vsqx_pitch_smooth_window,
        "f0_floor": f0_floor,
        "f0_ceil": f0_ceil,
    }

mfa_is_running = False

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIST = (BASE_DIR.parent / "frontend" / "dist").resolve()
WORK_DIR = (BASE_DIR / "work").resolve()

WINDOWS_SAFE_PATH_LIMIT = 248
WORK_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=str(FRONTEND_DIST), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024
CORS(app, supports_credentials=True)

mfa_processor = MFAProcessor()
pipeline = AudioProcessingPipeline(str(WORK_DIR))
from threading import Thread, Lock
from datetime import datetime

JOB_LOCK = Lock()
JOBS = {}

# 「导入字幕」批量拆分会话：session_id -> {"dir": str, "slices": [wav_path, ...]}
# 生命周期很短（用户拆分后立刻在前端下载各切片、随后调用 /cleanup 删除），
# 不需要像 JOBS 那样做状态轮询，纯粹是切片路径的登记表。
SUBTITLE_IMPORT_SESSIONS: Dict[str, Dict] = {}

def set_job(job_id: str, **kwargs):
    with JOB_LOCK:
        JOBS[job_id] = {
            **JOBS.get(job_id, {}),
            **kwargs,
        }


def get_job(job_id: str):
    with JOB_LOCK:
        return JOBS.get(job_id)


# ─────────────────────────────────────────────────────────────────────────────
# TTS 分段预览缓存："预览"按钮只做 TTS 合成（tts_processor.
# synthesize_segments_only），不做 Qwen3-FA 对齐；这里把合成产物（分句
# 音频目录 + 最终使用的句子列表 + 拼接后的完整音频路径）按 preview_id 缓存
# 起来，供随后点击"开始处理"时直接复用——不用把同一段文本合成两遍。
#
# 复用条件很严格：/api/tts/process 会比对当前提交的 text/engine/voice/
# rate/pitch/volume/language 与缓存条目是否完全一致，任何一项对不上都视为
# 预览已过期，直接忽略 preview_id，退回"先合成再对齐"的完整流程（对应用户
# 没有手动点过预览、或者点完预览后又改动过文本/参数的情况）。
#
# 缓存容量很小（同一时间基本只有一个用户在用这一个面板），这里只做简单的
# "超过上限就清掉最旧的" + "被消费后立即删除" 处理，不需要引入过期任务。
_TTS_PREVIEW_CACHE: Dict[str, dict] = {}
_TTS_PREVIEW_LOCK = Lock()
_TTS_PREVIEW_MAX_ENTRIES = 8


def _tts_preview_cleanup_dir(entry: dict) -> None:
    """删除一条预览缓存对应的磁盘产物（分句音频目录 + 拼接后的 WAV）。"""
    segments_dir = entry.get("segments_dir")
    if segments_dir:
        shutil.rmtree(segments_dir, ignore_errors=True)
    wav_path = entry.get("wav_path")
    if wav_path:
        try:
            Path(wav_path).unlink(missing_ok=True)
        except Exception:
            pass


def _tts_preview_store(entry: dict) -> str:
    preview_id = uuid.uuid4().hex
    with _TTS_PREVIEW_LOCK:
        _TTS_PREVIEW_CACHE[preview_id] = entry
        if len(_TTS_PREVIEW_CACHE) > _TTS_PREVIEW_MAX_ENTRIES:
            oldest_id = min(_TTS_PREVIEW_CACHE, key=lambda k: _TTS_PREVIEW_CACHE[k]["created_at"])
            if oldest_id != preview_id:
                stale = _TTS_PREVIEW_CACHE.pop(oldest_id, None)
                if stale:
                    _tts_preview_cleanup_dir(stale)
    return preview_id


def _tts_preview_take(preview_id: str, expected: dict) -> Optional[dict]:
    """
    取出并从缓存里移除一条预览记录，仅当其合成参数与当前提交的 expected
    完全一致时才返回；否则返回 None（调用方应退回完整合成+对齐流程）。
    一旦取出（无论后续对齐成功与否），这条缓存记录都不再保留。
    """
    if not preview_id:
        return None
    with _TTS_PREVIEW_LOCK:
        entry = _TTS_PREVIEW_CACHE.pop(preview_id, None)
    if not entry:
        return None
    for key, value in expected.items():
        if entry.get(key) != value:
            _tts_preview_cleanup_dir(entry)
            return None
    return entry


# ─────────────────────────────────────────────────────────────────────────────
# VSQX 声库自动选择
# ─────────────────────────────────────────────────────────────────────────────
_VSQX_SINGER_MAP = {
    # 完整处理模式（mode="full"）——按语种选择
    # 元组格式：(声库名, 声库ID, Bank Select编号)
    # Bank Select 由 VOCALOID4 在安装时分配，与安装顺序有关，下列值为常见默认值
    "en":  ("MIKU_V4_English",         "BMLTD846MLYP2MEK", 1),
    "eng": ("MIKU_V4_English",         "BMLTD846MLYP2MEK", 1),
    "ja":  ("MIKU_V4X_Original_EVEC",  "BCNFCY43LB2LZCD4", 0),
    "jpn": ("MIKU_V4X_Original_EVEC",  "BCNFCY43LB2LZCD4", 0),
    "ko":  ("SeeU_SV01_KOR",           "BX77CNBZLBPHZX97", 2),
    "kor": ("SeeU_SV01_KOR",           "BX77CNBZLBPHZX97", 2),
    # 中文 / 粤语 → 默认（回退值）
}
_VSQX_DEFAULT_FULL          = ("MIKU_V4_Chinese",        "BNGE7CP7EMTRSNC3", 4)  # cmn / yue / 其他
_VSQX_DEFAULT_PROJECT_ONLY  = ("MIKU_V4X_Original_EVEC", "BCNFCY43LB2LZCD4", 0)  # 仅生成工程固定日语声库


def _select_vsqx_singer(language: str, mode: str = "full"):
    """
    按语种（language）和处理模式（mode）返回 (vsqx_singer, vsqx_singer_id, vsqx_singer_bs)。

    完整处理 (mode="full"):
      en / eng  →  MIKU_V4_English        / BMLTD846MLYP2MEK / bs=1
      ja / jpn  →  MIKU_V4X_Original_EVEC / BCNFCY43LB2LZCD4 / bs=0
      ko / kor  →  SeeU_SV01_KOR          / BX77CNBZLBPHZX97 / bs=2
      其他      →  MIKU_V4_Chinese         / BNGE7CP7EMTRSNC3 / bs=4

    仅生成工程 (mode="project_only"):
      固定       →  MIKU_V4X_Original_EVEC / BCNFCY43LB2LZCD4 / bs=0

    注意：Bank Select (bs) 由 VOCALOID4 在安装声库时自动分配，与安装顺序相关。
    上列 bs 值为常见默认值，若用户系统上声库 bs 编号不同，需在前端手动传入 vsqx_singer_bs 覆盖。
    """
    if mode == "project_only":
        return _VSQX_DEFAULT_PROJECT_ONLY
    return _VSQX_SINGER_MAP.get((language or "").lower(), _VSQX_DEFAULT_FULL)

def abs_norm(path: str) -> str:
    return os.path.abspath(os.path.normpath(path))


def path_len(path: str) -> int:
    return len(abs_norm(path))


def sanitize_stem(name: str) -> str:
    stem = os.path.splitext(os.path.basename(name))[0]
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)
    stem = re.sub(r"\s+", "_", stem).strip("._ ")
    return stem or uuid.uuid4().hex[:12]


def fit_stem_to_limit(base_dir: str, stem: str, limit: int = WINDOWS_SAFE_PATH_LIMIT) -> str:
    base_abs = abs_norm(base_dir)
    fixed_overhead = len(base_abs) + 1 + 4
    max_stem_len = limit - fixed_overhead
    if max_stem_len < 8:
        raise ValueError("工作目录太深，无法在 248 字符限制内保存文件。")

    if len(stem) > max_stem_len:
        stem = stem[:max_stem_len].rstrip("._ ")
    return stem or uuid.uuid4().hex[:12]


def build_job_paths(original_filename: str):
    stem = sanitize_stem(original_filename)
    
    # 【优化】为文件名注入 6 位随机标识符，彻底避免连续点击时发生文件覆盖/锁死冲突
    unique_suffix = uuid.uuid4().hex[:6]
    stem = f"{stem}_{unique_suffix}"
    
    stem = fit_stem_to_limit(str(WORK_DIR), stem)

    wav_path = WORK_DIR / f"{stem}.wav"
    lab_path = WORK_DIR / f"{stem}.lab"

    if path_len(str(wav_path)) > WINDOWS_SAFE_PATH_LIMIT or path_len(str(lab_path)) > WINDOWS_SAFE_PATH_LIMIT:
        raise ValueError("生成后的文件路径仍然超过 248 字符，请把项目目录放得更浅一些。")

    return stem, wav_path, lab_path

@app.after_request
def disable_keepalive(response):
    """
    强制告诉浏览器关闭当前连接，不复用 TCP Socket。
    彻底解决 Werkzeug 开发服务器在连续上传大文件时，因 Keep-Alive 复用导致的 Connection Reset (Failed to fetch) 问题。
    """
    response.headers["Connection"] = "close"
    return response

@app.errorhandler(404)
@app.errorhandler(405)
def api_error_as_json(e):
    """
    对 /api/* 路径的 404/405 错误统一返回 JSON，而不是回退到 SPA 的 index.html。
    避免前端 fetch().json() 因为收到 HTML (<!doctype ...) 而抛出
    'Unexpected token < is not valid JSON' 的问题。
    """
    if request.path.startswith("/api/"):
        code = getattr(e, "code", 500) or 500
        return jsonify({
            "error": getattr(e, "name", "Error"),
            "message": getattr(e, "description", str(e)),
        }), code
    raise e


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if path.startswith("api/"):
        abort(404)

    full_path = (FRONTEND_DIST / path).resolve()
    if path and full_path.is_file():
        return send_from_directory(str(FRONTEND_DIST), path)

    index_path = FRONTEND_DIST / "index.html"
    if index_path.is_file():
        return send_from_directory(str(FRONTEND_DIST), "index.html")

    return jsonify({
        "error": "前端文件未找到",
        "message": "请在 frontend/ 目录下执行 npm install && npm run build",
        "expected_dir": str(FRONTEND_DIST)
    }), 404


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "version": "2.0.0",
        "app": "Audio Processing Aligner with MFA + PyWORLD"
    }), 200


@app.route("/api/debug/runtime", methods=["GET"])
def debug_runtime():
    return jsonify({
        "python_executable": sys.executable,
        "python_version": sys.version,
        "conda_prefix": os.environ.get("CONDA_PREFIX", ""),
        "env_dir": str(MFAChecker.env_dir()),
    }), 200


@app.route("/api/mfa/status", methods=["GET"])
def mfa_status():
    return jsonify(MFAChecker.get_status()), 200


@app.route("/api/pipeline/status", methods=["GET"])
def pipeline_status():
    try:
        status = pipeline.get_status()
        return jsonify({
            "success": True,
            "status": status
        }), 200
    except Exception as e:
        logger.error(f"查询状态失败: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# 新增
@app.route("/api/aligner/status", methods=["GET"])
def aligner_status():
    """检查所有对齐后端（MFA / Qwen3）的可用状态"""
    try:
        mfa_ok, mfa_msg = MFAChecker.check_mfa_installed()
        alt_status = get_alt_aligner_status()
        return jsonify({
            "success": True,
            "backends": {
                "mfa": {
                    "available": mfa_ok,
                    "message": mfa_msg,
                    "requires_text": True,
                    "description": "Montreal Forced Aligner（传统音素对齐）",
                },
                **alt_status,
            }
        }), 200
    except Exception as e:
        logger.error(f"查询对齐器状态失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/align", methods=["POST"]) # 请替换为你实际的对齐接口路由
def align_audio():
    global mfa_is_running # 必须声明 global 才能修改这个全局变量
    
    # 1. 检查锁状态
    if mfa_is_running:
        return jsonify({
            "success": False, 
            "error": "上一个对齐任务正在运行中，请勿重复提交或频繁刷新页面！"
        }), 429
        
    try:
        # 2. 上锁
        mfa_is_running = True
        logger.info("MFA 对齐任务开始执行，全局锁已启用。")
        
        # 3. 这里执行你原本的对齐逻辑
        # ... 原来的 pipeline 运行代码 ...
        
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"对齐失败: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        # 4. 无论成功还是失败，最终一定要释放锁
        mfa_is_running = False
        logger.info("MFA 对齐任务结束，全局锁已释放。")

@app.route("/api/pipeline/job/<job_id>", methods=["GET"])
def pipeline_job_status(job_id):
    job = get_job(job_id)

    if not job:
        return jsonify({
            "success": False,
            "error": "任务不存在"
        }), 404

    return jsonify({
        "success": True,
        "job": job
    }), 200


@app.route("/api/pipeline/formats", methods=["GET"])
def pipeline_formats():
    """获取支持的输出格式"""
    try:
        formats = pipeline.get_supported_formats()
        return jsonify({
            "success": True,
            "formats": formats
        }), 200
    except Exception as e:
        logger.error(f"查询格式失败: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# =====================================================================
# 单词 → 音素 用户词典（功能 1）
# 支持任意数量、任意命名的独立词典（不支持 OpenUTAU）。
# =====================================================================

@app.route("/api/dictionary", methods=["GET"])
def dictionary_list_all():
    """列出所有独立词典（{name, notation, count}）。"""
    return jsonify({"success": True, "dictionaries": dictionary_manager.list_dictionaries()}), 200


@app.route("/api/dictionary", methods=["POST"])
def dictionary_create():
    """创建一本新的独立词典。body: {"name": "...", "notation": "synthesizerv"|"vocaloid"}"""
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name", "")
    notation = data.get("notation", "synthesizerv")
    try:
        created = dictionary_manager.create_dictionary(name, notation)
        return jsonify({"success": True, "dictionary": created}), 201
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/dictionary/<source>", methods=["DELETE"])
def dictionary_delete_all(source):
    """删除整本独立词典。"""
    existed = dictionary_manager.delete_dictionary(source)
    if not existed:
        return jsonify({"success": False, "error": "词典不存在"}), 404
    return jsonify({"success": True}), 200


@app.route("/api/dictionary/<source>", methods=["GET"])
def dictionary_list(source):
    """列出指定来源词典的全部词条"""
    try:
        entries = dictionary_manager.list_entries(source)
        return jsonify({
            "success": True,
            "source": source,
            "entries": entries,
            "count": len(entries),
        }), 200
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/dictionary/<source>/entry", methods=["POST"])
def dictionary_upsert(source):
    """新增或更新单个词条"""
    data = request.get_json(force=True, silent=True) or {}
    word = data.get("word", "")
    phonemes = data.get("phonemes", "")
    try:
        dictionary_manager.upsert_entry(source, word, phonemes)
        return jsonify({"success": True}), 200
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/dictionary/<source>/entry", methods=["DELETE"])
def dictionary_delete(source):
    """删除单个词条"""
    data = request.get_json(force=True, silent=True) or {}
    word = data.get("word", "") or request.args.get("word", "")
    try:
        existed = dictionary_manager.delete_entry(source, word)
        if not existed:
            return jsonify({"success": False, "error": "词条不存在"}), 404
        return jsonify({"success": True}), 200
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400


def _safe_filename_component(name: str) -> str:
    """把任意词典名转成可安全放进 Content-Disposition 的文件名片段。"""
    import re as _re
    cleaned = _re.sub(r'[\\/:*?"<>|\r\n]+', "_", (name or "dictionary").strip()) or "dictionary"
    return cleaned[:80]


@app.route("/api/dictionary/<source>/export", methods=["GET"])
def dictionary_export(source):
    """导出词典。?format=json（默认，直接返回 JSON）或 ?format=csv（文件下载）"""
    fmt = request.args.get("format", "json").lower()
    safe_name = _safe_filename_component(source)
    try:
        if fmt == "csv":
            csv_text = dictionary_manager.export_csv(source)
            # 加 UTF-8 BOM，避免 Excel 等工具把含中文/日文音素的 CSV 误判成
            # 其他编码打开后出现乱码（这也是此前"CSV 词典无法正常使用"
            # 反馈的常见成因之一）。
            csv_bytes = ("\ufeff" + csv_text).encode("utf-8")
            return Response(
                csv_bytes,
                mimetype="text/csv",
                headers={
                    "Content-Disposition": (
                        f"attachment; filename={safe_name}_dictionary.csv; "
                        f"filename*=UTF-8''{quote(safe_name)}_dictionary.csv"
                    )
                },
            )
        data = dictionary_manager.export_json(source)
        return jsonify({"success": True, "source": source, "data": data}), 200
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400


def _extract_entries_from_json_payload(payload: object, source: str):
    """
    尽量宽松地从导入的 JSON 中解析出 {WORD: phonemes} 词条字典。

    支持三种常见形状：
      1) 扁平格式：{"WORD": "phones", ...}
      2) 本模块 export_json() 的单词典格式：{"notation": "...", "entries": {...}}
      3) 旧版 / 带词典名包裹的格式：{"<某词典名>": {"notation":...,"entries":{...}}}
         或 {"<某词典名>": {"WORD": "phones", ...}}
         —— 包裹键若与目标词典名 source 不同也没关系，只要值是合法词条结构
         就按第一个匹配的取用，方便用户重命名后再导入。
    """
    if not isinstance(payload, dict) or not payload:
        return None

    # 形状 2
    if isinstance(payload.get("entries"), dict):
        return payload["entries"]

    # 形状 3：优先精确匹配 source，否则取第一个看起来像词典的值
    candidates = [payload[source]] if source in payload else list(payload.values())
    for inner in candidates:
        if isinstance(inner, dict):
            if isinstance(inner.get("entries"), dict):
                return inner["entries"]
            if inner and all(not isinstance(v, dict) for v in inner.values()):
                return inner

    # 形状 1：扁平词条字典（值全部是字符串/非 dict）
    if all(not isinstance(v, dict) for v in payload.values()):
        return payload

    return None


@app.route("/api/dictionary/<source>/import", methods=["POST"])
def dictionary_import(source):
    """
    导入词典。若目标词典名不存在会自动创建。支持两种方式：
      1) multipart/form-data 上传文件（file 字段，.csv 或 .json），
         可选 overwrite / notation 表单字段（notation 仅在自动创建新词典
         时生效，默认 true / synthesizerv）
      2) application/json 请求体
         {"entries": {"WORD": "phones", ...}, "overwrite": true, "notation": "..."}
    """
    try:
        if "file" in request.files:
            f = request.files["file"]
            overwrite = request.form.get("overwrite", "true").lower() != "false"
            notation = request.form.get("notation", "synthesizerv")
            filename = (f.filename or "").lower()
            raw = f.read().decode("utf-8-sig")

            if filename.endswith(".json"):
                payload = json.loads(raw)
                entries = _extract_entries_from_json_payload(payload, source)
                if entries is None:
                    return jsonify({"success": False, "error": "JSON 格式不正确，未能解析出词条"}), 400
                added, updated = dictionary_manager.bulk_import(source, entries, overwrite=overwrite, notation=notation)
            else:
                added, updated = dictionary_manager.import_csv_text(source, raw, overwrite=overwrite, notation=notation)
        else:
            payload = request.get_json(force=True, silent=True) or {}
            entries = payload.get("entries", {})
            overwrite = bool(payload.get("overwrite", True))
            notation = payload.get("notation", "synthesizerv")
            added, updated = dictionary_manager.bulk_import(source, entries, overwrite=overwrite, notation=notation)

        return jsonify({"success": True, "added": added, "updated": updated}), 200
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"词典导入失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": f"导入失败: {str(e)}"}), 500


# =====================================================================
# 文本优化弹窗（"优化文本"按钮）：纯文本转换，只影响调用方传入的这一段
# 文本本身，不落盘、不触碰 MFA / TTS / 对齐 / 词典等任何其它后端流程。
# =====================================================================

@app.route("/api/text/optimize", methods=["POST"])
def text_optimize():
    """
    文本优化弹窗统一入口。body: {"text": "...", "action": "smart" |
    "number_only" | "digit_to_words" | "symbol_only" | "add_spaces" |
    "strip_symbols" | "newline_after_comma" | "newline_after_period" |
    "newline_every_n" | "hyphen_to_space", "language":
    "cmn"|"yue"|"eng"|"jpn"|"kor"（仅
    smart/number_only/digit_to_words/symbol_only 需要，其余 action 与
    语种无关，可不传）, "n": 2（仅 newline_every_n 需要，表示"每几句插入
    一次换行"，不传时默认 2）}
    """
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "")
    action = data.get("action", "")
    language = data.get("language", "zh")
    n = data.get("n", 2)
    result = text_processor.process_text(text, action, language, n)
    return jsonify(result), (200 if result.get("success") else 400)


# =====================================================================
# 英文 G2P：将英文文本转换为 ARPABET 音素序列
# =====================================================================

_g2p_en_instance = None

def _get_g2p_en():
    """惰性加载 g2p_en.G2p 实例（首次调用较慢，之后复用）。"""
    global _g2p_en_instance
    if _g2p_en_instance is None:
        from g2p_en import G2p
        _g2p_en_instance = G2p()
    return _g2p_en_instance


@app.route("/api/english/extract-g2p", methods=["POST"])
def english_extract_g2p():
    """
    英文文本 -> ARPABET 音素转换接口。
    请求体: { text, case_sensitive, include_numbers, split_by_space }
    返回体: { results: [ { word, arpa }, ... ] }
    """
    try:
        data = request.get_json(force=True) or {}
        text = (data.get("text") or "").strip()
        case_sensitive = bool(data.get("case_sensitive", False))
        include_numbers = bool(data.get("include_numbers", True))
        split_by_space = bool(data.get("split_by_space", True))

        if not text:
            return jsonify({"results": []})

        if split_by_space:
            words = [w for w in text.split(" ") if w]
        else:
            words = re.findall(r"[A-Za-z']+|\d+", text)

        g2p = _get_g2p_en()
        results = []
        for raw_word in words:
            lookup_word = raw_word if case_sensitive else raw_word.lower()

            if raw_word.isdigit() and not include_numbers:
                results.append({"word": raw_word, "arpa": ""})
                continue

            try:
                phonemes = g2p(lookup_word)
                # g2p_en 会在词间插入空格 token，以及标点原样返回，需过滤为纯音素
                arpa_tokens = [p for p in phonemes if p != " " and re.match(r"^[A-Z]+[0-2]?$", p)]
                arpa = " ".join(arpa_tokens)
            except Exception as inner_e:
                logging.warning(f"G2P conversion failed for word '{raw_word}': {inner_e}")
                arpa = ""

            results.append({"word": raw_word, "arpa": arpa})

        return jsonify({"results": results})

    except Exception as e:
        logging.exception("english_extract_g2p failed")
        return jsonify({"error": str(e)}), 500


# =====================================================================
# 应用设置：模型自动更新 / 镜像站下载（功能 2）
# =====================================================================

# Qwen3-ASR / NeMo Forced Aligner 微服务的固定本地地址（与
# alt_aligners.py 里 Qwen3ASRAligner.DEFAULT_ENDPOINT /
# NeMoForcedAligner.DEFAULT_ENDPOINT 使用的端口保持一致）。
_QWEN3_BASE_URL = "http://127.0.0.1:5001"
_NEMO_BASE_URL = "http://127.0.0.1:5002"


def _restart_microservice(base_url: str, display_name: str) -> Dict[str, str]:
    """
    尝试让一个独立运行的微服务（Qwen3-ASR / NeMo）自我重启，以应用刚保存的
    模型下载设置（HF_HUB_OFFLINE / HF_ENDPOINT 只在进程启动时读取一次）。

    这两个微服务是否在运行完全取决于用户是否手动开了对应的独立
    conda/venv 终端窗口——没开不算错误，只是"无需重启"。

    Returns
    -------
    {"status": "restarted" | "not_running" | "failed", "detail": str}
    """
    try:
        # 先探活：微服务可能压根没启动，这是正常情况
        requests.get(base_url + "/", timeout=1.5)
    except requests.exceptions.RequestException:
        return {"status": "not_running", "detail": ""}

    try:
        requests.post(base_url + "/restart", timeout=3)
        logger.info(f"已触发 {display_name} 微服务重启 ({base_url})")
        return {"status": "restarted", "detail": ""}
    except requests.exceptions.RequestException as e:
        logger.warning(f"触发 {display_name} 微服务重启失败: {e}")
        return {"status": "failed", "detail": str(e)}


@app.route("/api/settings", methods=["GET"])
def get_settings():
    try:
        settings = app_settings.load_settings()
        return jsonify({"success": True, "settings": settings}), 200
    except Exception as e:
        logger.error(f"读取设置失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/settings", methods=["POST"])
def update_settings():
    """
    更新设置并写入共享配置文件，然后自动尝试重启 Qwen3-ASR / NeMo 两个
    独立微服务进程，让 HF_HUB_OFFLINE / HF_ENDPOINT 立即生效——不再需要
    用户手动去关闭再重开那两个独立终端窗口。

    每个微服务是否成功重启（或本来就没在运行）会在返回的 "restart"
    字段里分别标出，前端据此展示准确的结果提示。
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        settings = app_settings.save_settings(data)

        # 主服务（app.py 自己）的命令提示符窗口显示/隐藏可以立即生效，
        # 不需要像 HF_HUB_OFFLINE / HF_ENDPOINT 那样重启进程——直接对当前
        # 进程调用一次即可。Qwen3 / NeMo 两个微服务则随下面的自动重启一并
        # 应用最新的 hide_console_window 值（新进程启动时会重新读取）。
        try:
            app_settings.apply_console_visibility()
        except Exception as e:
            logger.warning(f"⚠️  设置控制台窗口显示状态失败（不影响服务本身运行）: {e}")

        restart_result = {
            "qwen3": _restart_microservice(_QWEN3_BASE_URL, "Qwen3-ASR"),
            "nemo": _restart_microservice(_NEMO_BASE_URL, "NeMo Forced Aligner"),
        }

        return jsonify({
            "success": True,
            "settings": settings,
            "restart": restart_result,
            "message": "设置已保存",
        }), 200
    except Exception as e:
        logger.error(f"保存设置失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
def upload_wav_and_text():
    """上传音频和文本（仅保存，不处理）"""
    try:
        if "audio_file" not in request.files:
            return jsonify({"error": "缺少 audio_file"}), 400
        if "text" not in request.form:
            return jsonify({"error": "缺少 text"}), 400

        audio_file = request.files["audio_file"]
        text = request.form.get("text", "").strip()

        if not audio_file or not text:
            return jsonify({"error": "输入无效"}), 400

        stem, wav_path, lab_path = build_job_paths(audio_file.filename or "audio.wav")

        audio_file.save(str(wav_path))
        lab_path.write_text(text + "\n", encoding="utf-8")

        return jsonify({
            "success": True,
            "stem": stem,
            "wav_path": str(wav_path),
            "lab_path": str(lab_path),
            "lab": text,
            "lab_content": text,
            "message": "已保存同名 wav / lab"
        }), 200

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("上传保存失败: %s", e, exc_info=True)
        return jsonify({"error": f"保存失败: {str(e)}"}), 500


@app.route("/api/mfa/process", methods=["POST"])
def process_mfa():
    """MFA 自动标注处理"""
    try:
        if "audio_file" not in request.files:
            return jsonify({"error": "缺少 audio_file"}), 400
        if "text" not in request.form:
            return jsonify({"error": "缺少 text"}), 400

        audio_file = request.files["audio_file"]
        text = request.form.get("text", "").strip()
        language = request.form.get("language", "cmn")

        if not audio_file or not text:
            return jsonify({"error": "输入无效"}), 400

        result = mfa_processor.process(audio_file, text, language)

        if result.get("success"):
            return jsonify(result), 200

        return jsonify({
            "success": False,
            "error": result.get("error", "处理失败"),
            "processing_time_ms": result.get("processing_time", 0)
        }), 500

    except Exception as e:
        logger.error("处理错误: %s", e, exc_info=True)
        return jsonify({"error": f"处理出错: {str(e)}"}), 500

def run_pipeline_job(
    job_id: str,
    wav_path: str,
    text: str,
    language: str,
    output_format: str,
    project_title: str,
    bpm: float,
    base_pitch: int,
    f0_method: str,
    f0_smooth: bool,
    f0_smooth_window: int,
    use_double_precision: bool,
    f0_floor: float,
    f0_ceil: float,
    refine_pitch: bool,
    export_pitch_line: bool,
    f0_device: str = "auto",
    crepe_model: str = "full",
    aligner_backend: str = "mfa",
    whisperx_model: str = "large-v3",
    nemo_model: str = "",
    english_word_align: bool = False,
    vsqx_singer: str = "MIKU_V4_Chinese",
    vsqx_singer_id: str = "BNGE7CP7EMTRSNC3",
    vsqx_singer_bs: int = 4,
    word_phoneme_map: bool = False,
    dict_source: str = "default",
    vsqx_pitch_smooth_window: int = 5,
    whisperx_batch_size: int = 16,
    aligner_device: str = "auto",
    align_pitch_shift_semitones: float = 0.0,
):
    try: # <--- Added the missing try block here
        class FileStorageWrapper:
            def __init__(self, local_path):
                self.path = os.path.abspath(local_path)
                self.filename = os.path.basename(local_path)

            def save(self, dst):
                # 如果目标路径和当前文件路径不同，则执行复制
                import shutil
                if os.path.abspath(dst) != self.path:
                    shutil.copy(self.path, dst)

            def seek(self, *args, **kwargs):
                # 兼容 pipeline.py 里的 audio_file.seek(0)
                pass

        # 将路径字符串包装为兼容的对象
        compat_audio_file = FileStorageWrapper(wav_path)
        # ========================================

        result = pipeline.process_full(
            audio_file=compat_audio_file,  # 这里改为传入包装后的对象
            text=text,
            language=language,
            output_format=output_format,
            project_title=project_title,
            bpm=bpm,
            base_pitch=base_pitch,
            f0_method=f0_method,
            f0_smooth=f0_smooth,
            f0_smooth_window=f0_smooth_window,
            use_double_precision=use_double_precision,
            f0_floor=f0_floor,
            f0_ceil=f0_ceil,
            refine_pitch=refine_pitch,
            export_pitch_line=export_pitch_line,
            vsqx_pitch_smooth_window=vsqx_pitch_smooth_window,
            f0_device=f0_device,
            crepe_model=crepe_model,
            aligner_backend=aligner_backend,
            aligner_device=aligner_device,
            whisperx_model=whisperx_model,
            whisperx_batch_size=whisperx_batch_size,
            nemo_model=(nemo_model or None),
            english_word_align=english_word_align,
            vsqx_singer=vsqx_singer,
            vsqx_singer_id=vsqx_singer_id,
            vsqx_singer_bs=vsqx_singer_bs,
            word_phoneme_map=word_phoneme_map,
            dict_source=dict_source,
            align_pitch_shift_semitones=align_pitch_shift_semitones,
        )

        if result.get("success"):
            set_job(
                job_id,
                status="done",
                finished_at=datetime.now().isoformat(),
                result=result,
            )
        else:
            set_job(
                job_id,
                status="failed",
                finished_at=datetime.now().isoformat(),
                error=result.get("error"),
                result=result,
            )

    except Exception as e:
        logger.exception("后台任务异常")

        set_job(
            job_id,
            status="failed",
            finished_at=datetime.now().isoformat(),
            error=str(e),
        )


@app.route("/api/pipeline/full", methods=["POST"])
def pipeline_full_process():
    """
    MFA + F0 + 工程文件生成
    异步后台任务版
    """
    try:
        if "audio_file" not in request.files:
            return jsonify({"error": "缺少 audio_file"}), 400

        if "text" not in request.form:
            return jsonify({"error": "缺少 text"}), 400

        audio_file = request.files["audio_file"]
        text = request.form.get("text", "").strip()

        language = request.form.get("language", "cmn")
        output_format = request.form.get("format", "sv")
        project_title = request.form.get("title", "Project")

        bpm = float(request.form.get("bpm", 120))
        base_pitch = int(request.form.get("base_pitch", 60))

        f0_method = request.form.get("f0_method", "dio")
        f0_smooth = request.form.get("f0_smooth", "true").lower() == "true"

        f0_device = request.form.get("f0_device", "auto")
        crepe_model = request.form.get("crepe_model", "full")

        f0_smooth_window = int(
            request.form.get("f0_smooth_window", 5)
        )

        use_double_precision = (
            request.form.get("precision", "single").lower()
            == "double"
        )

        f0_floor = float(request.form.get("f0_floor", 71.0))
        f0_ceil = float(request.form.get("f0_ceil", 800.0))

        # 【修复】前端发送的是 auto_note_pitch，而非 refine_pitch
        refine_pitch = (
            request.form.get("auto_note_pitch", "false").lower()
            == "true"
        )

        # 【修复】前端发送的 export_pitch_line 决定是否将 F0 曲线写入工程文件
        export_pitch_line = (
            request.form.get("export_pitch_line", "true").lower()
            == "true"
        )

        # VSQX PIT 曲线平滑窗口（仅 output_format == vsqx 时生效，其余格式忽略）
        vsqx_pitch_smooth_window = int(
            request.form.get("vsqx_pitch_smooth_window", 5)
        )

        aligner_backend = request.form.get("aligner_backend", "mfa")
        if aligner_backend not in ("mfa", "whisperx", "qwen3_asr", "qwen3_aligner", "nemo_aligner"):
            aligner_backend = "mfa"

        whisperx_model = request.form.get("whisperx_model", "large-v3")
        if whisperx_model not in ("large-v3", "large-v3-turbo", "large-v2", "medium", "small", "base", "tiny"):
            whisperx_model = "large-v3"

        # WhisperX 推理批大小：仅在 device 实际解析为 cuda 时才真正影响显存
        # 占用，CPU 模式下该值基本不产生效果，但依然接受并透传（不强制
        # 覆盖），避免前端/后端逻辑不一致。夹到 [1, 64] 防止误输入极端值。
        try:
            whisperx_batch_size = int(request.form.get("whisperx_batch_size", 16))
        except (TypeError, ValueError):
            whisperx_batch_size = 16
        whisperx_batch_size = max(1, min(whisperx_batch_size, 64))

        # NeMo Forced Aligner 模型覆盖（可选）：留空则由 NeMoForcedAligner
        # 按语言使用内置默认模型（见 alt_aligners.NeMoForcedAligner.LANGUAGE_MODELS）
        nemo_model = request.form.get("nemo_model", "").strip()

        english_word_align = request.form.get("english_word_align", "false").lower() == "true"
        word_phoneme_map   = request.form.get("word_phoneme_map",   "false").lower() == "true"

        # 对齐辅助移调（半音）：仅影响送入对齐后端的临时音频副本，不影响
        # F0 提取或最终工程文件音高。
        align_pitch_shift_semitones = _parse_align_pitch_shift(
            request.form.get("align_pitch_shift_semitones", 0)
        )

        # 【解耦】前端不再要求用户手动开启"英语单词级对齐"开关才能使用
        # "单词映射音素"/词典来源功能——只要用户开启了 word_phoneme_map，
        # 就在后端自动补上 english_word_align=True（日语除外，前端本就不
        # 提供该开关），确保对齐阶段产出整词级 LAB，词典查词才有意义；
        # 用户仍可手动开启 english_word_align 以获得其独立效果（英语单词
        # 直接输出而不拆分为 ARPABET），两者互不冲突，取或即可。
        if word_phoneme_map and language != "jpn":
            english_word_align = True

        dict_source = _normalize_dict_source(request.form.get("dict_source", "default"))

        vsqx_singer    = request.form.get("vsqx_singer",    "MIKU_V4_Chinese")
        vsqx_singer_id = request.form.get("vsqx_singer_id", "BNGE7CP7EMTRSNC3")
        vsqx_singer_bs = int(request.form.get("vsqx_singer_bs", 4))

        # 输出格式为 vsqx 时，按语种自动覆盖声库（忽略前端传值，保证正确性）
        if output_format == "vsqx":
            vsqx_singer, vsqx_singer_id, vsqx_singer_bs = _select_vsqx_singer(language, "full")
            logger.info(
                "VSQX 声库自动选择 [language=%s]: %s / %s / bs=%d",
                language, vsqx_singer, vsqx_singer_id, vsqx_singer_bs,
            )

        stem, wav_path, lab_path = build_job_paths(audio_file.filename or "audio.wav")

        audio_file.save(str(wav_path))

        lab_path.write_text(
            text + "\n",
            encoding="utf-8"
        )

        job_id = uuid.uuid4().hex

        set_job(
            job_id,
            status="queued",
            created_at=datetime.now().isoformat(),
        )

        Thread(
            target=run_pipeline_job,
            daemon=True,
            args=(
                job_id,
                str(wav_path),
                text,
                language,
                output_format,
                project_title,
                bpm,
                base_pitch,
                f0_method,
                f0_smooth,
                f0_smooth_window,
                use_double_precision,
                f0_floor,
                f0_ceil,
                refine_pitch,
                export_pitch_line,
                f0_device,
                crepe_model,
                aligner_backend,
                whisperx_model,
                nemo_model,
                english_word_align,
                vsqx_singer,
                vsqx_singer_id,
                vsqx_singer_bs,
                word_phoneme_map,
                dict_source,
                vsqx_pitch_smooth_window,
                whisperx_batch_size,
            ),
            # 用关键字参数追加新增字段，避开 run_pipeline_job 尾部
            # aligner_device 的位置参数陷阱（该路由此前一直没有解析/透传
            # aligner_device，一直隐性按默认值 "auto" 运行——这是已存在的
            # 行为，这里不额外修正，只保证新增字段不会因为位置错位而串位）。
            kwargs=dict(align_pitch_shift_semitones=align_pitch_shift_semitones),
        ).start()

        return jsonify(
            {
                "success": True,
                "job_id": job_id,
                "status": "queued",
            }
        ), 202

    except Exception as e:
        logger.exception("完整流程启动失败")
        return jsonify({"error": str(e)}), 500
    

# =====================================================================
# 替换原有的 pipeline_mfa_only 函数，新增 run_mfa_only_job 异步任务
# =====================================================================

def run_mfa_only_job(job_id: str, wav_path: str, text: str, language: str,
                     aligner_backend: str = "mfa", f0_device: str = "auto",
                     whisperx_model: str = "large-v3",
                     nemo_model: str = "",
                     english_word_align: bool = False,
                     whisperx_batch_size: int = 16,
                     aligner_device: Optional[str] = None,
                     align_pitch_shift_semitones: float = 0.0):
    try:
        set_job(
            job_id,
            status="running",
            started_at=datetime.now().isoformat(),
        )

        # 伪造 FileStorage 包装器，兼容 pipeline 的写入逻辑
        class FileStorageWrapper:
            def __init__(self, local_path):
                self.path = os.path.abspath(local_path)
                self.filename = os.path.basename(local_path)

            def save(self, dst):
                import shutil
                if os.path.abspath(dst) != self.path:
                    shutil.copy(self.path, dst)

            def seek(self, *args, **kwargs):
                pass

        compat_audio_file = FileStorageWrapper(wav_path)

        # 执行对齐标注
        result = pipeline.process_mfa_only(compat_audio_file, text, language,
                                           aligner_backend=aligner_backend,
                                           f0_device=f0_device,
                                           aligner_device=aligner_device,
                                           whisperx_model=whisperx_model,
                                           whisperx_batch_size=whisperx_batch_size,
                                           nemo_model=(nemo_model or None),
                                           english_word_align=english_word_align,
                                           align_pitch_shift_semitones=align_pitch_shift_semitones)

        if result.get("success"):
            set_job(
                job_id,
                status="done",
                finished_at=datetime.now().isoformat(),
                result=result,
            )
        else:
            set_job(
                job_id,
                status="failed",
                finished_at=datetime.now().isoformat(),
                error=result.get("error"),
                result=result,
            )

    except Exception as e:
        logger.exception("后台MFA任务异常")
        set_job(
            job_id,
            status="failed",
            finished_at=datetime.now().isoformat(),
            error=str(e),
        )


@app.route("/api/pipeline/mfa-only", methods=["POST"])
def pipeline_mfa_only():
    """仅执行 MFA 标注 (异步后台任务轮询版)"""
    try:
        if "audio_file" not in request.files:
            return jsonify({"error": "缺少 audio_file"}), 400

        audio_file = request.files["audio_file"]
        text = request.form.get("text", "").strip()
        language = request.form.get("language", "cmn")

        aligner_backend = request.form.get("aligner_backend", "mfa")
        if aligner_backend not in ("mfa", "whisperx", "qwen3_asr", "qwen3_aligner", "nemo_aligner"):
            aligner_backend = "mfa"
        f0_device = request.form.get("f0_device", "auto")

        # 对齐工具（WhisperX/Qwen3/NeMo）运行设备，与 f0_device（F0 提取设备）解耦。
        # 未提交该字段时传 None，让 pipeline._run_alignment 按约定回退到 f0_device。
        aligner_device = request.form.get("aligner_device", "").strip() or None

        whisperx_model = request.form.get("whisperx_model", "large-v3")
        if whisperx_model not in ("large-v3", "large-v3-turbo", "large-v2", "medium", "small", "base", "tiny"):
            whisperx_model = "large-v3"

        try:
            whisperx_batch_size = int(request.form.get("whisperx_batch_size", 16))
        except (TypeError, ValueError):
            whisperx_batch_size = 16
        whisperx_batch_size = max(1, min(whisperx_batch_size, 64))

        # NeMo Forced Aligner 模型覆盖（可选）：留空则由 NeMoForcedAligner
        # 按语言使用内置默认模型
        nemo_model = request.form.get("nemo_model", "").strip()

        english_word_align = request.form.get("english_word_align", "false").lower() == "true"

        # 对齐辅助移调（半音）：仅影响送入对齐后端的临时音频副本。
        align_pitch_shift_semitones = _parse_align_pitch_shift(
            request.form.get("align_pitch_shift_semitones", 0)
        )

        # WhisperX / Qwen3-ASR 支持纯 ASR 模式，文本可选；
        # Qwen3-ForcedAligner / NeMo Forced Aligner 都是强制对齐，必须提供参考文本
        text_optional = aligner_backend in ("whisperx", "qwen3_asr")
        if not audio_file or (not text and not text_optional):
            return jsonify({"error": "输入无效（MFA / Qwen3-ForcedAligner / NeMo Forced Aligner 模式需要文本）"}), 400

        # 1. 马上保存文件，生成路径
        stem, wav_path, lab_path = build_job_paths(audio_file.filename or "audio.wav")
        audio_file.save(str(wav_path))

        # 2. 创建任务 ID
        job_id = uuid.uuid4().hex
        set_job(
            job_id,
            status="queued",
            created_at=datetime.now().isoformat(),
        )

        logger.info(f"标注模式启动 (backend={aligner_backend})，投递后台任务: {job_id}")

        # 3. 启动后台线程执行耗时任务
        Thread(
            target=run_mfa_only_job,
            daemon=True,
            args=(job_id, str(wav_path), text, language, aligner_backend, f0_device,
                  whisperx_model, nemo_model, english_word_align, whisperx_batch_size,
                  aligner_device, align_pitch_shift_semitones),
        ).start()

        # 4. 立即返回 job_id 供前端轮询
        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": "queued",
            "aligner_backend": aligner_backend,
        }), 202

    except Exception as e:
        logger.error("标注模式异常: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


def run_project_only_job(
    job_id: str,
    wav_path: str,
    lab_path: str,
    output_format: str,
    project_title: str,
    bpm: float,
    base_pitch: int,
    f0_method: str,
    f0_smooth: bool,
    f0_smooth_window: int,
    use_double_precision: bool,
    f0_floor: float,
    f0_ceil: float,
    refine_pitch: bool,
    export_pitch_line: bool,
    f0_device: str,
    crepe_model: str,
    phoneme_mode: str,
    midi_path: str,           # MIDI 文件路径（空字符串 = 未导入）
    lyrics_text: str = "",
    vsqx_singer: str = "MIKU_V4_Chinese",
    vsqx_singer_id: str = "BNGE7CP7EMTRSNC3",
    vsqx_singer_bs: int = 4,
    word_phoneme_map: bool = False,
    dict_source: str = "default",
    vsqx_pitch_smooth_window: int = 5,
):
    try:
        set_job(
            job_id,
            status="running",
            started_at=datetime.now().isoformat(),
        )

        result = pipeline.process_project_only(
            wav_path=wav_path,
            lab_path=lab_path,
            output_format=output_format,
            project_title=project_title,
            bpm=bpm,
            base_pitch=base_pitch,
            f0_method=f0_method,
            f0_smooth=f0_smooth,
            f0_smooth_window=f0_smooth_window,
            use_double_precision=use_double_precision,
            f0_floor=f0_floor,
            f0_ceil=f0_ceil,
            refine_pitch=refine_pitch,
            export_pitch_line=export_pitch_line,
            vsqx_pitch_smooth_window=vsqx_pitch_smooth_window,
            f0_device=f0_device,
            crepe_model=crepe_model,
            phoneme_mode=phoneme_mode,
            midi_path=midi_path or None,
            vsqx_singer=vsqx_singer,
            vsqx_singer_id=vsqx_singer_id,
            vsqx_singer_bs=vsqx_singer_bs,
            word_phoneme_map=word_phoneme_map,
            dict_source=dict_source,
        )

        if result.get("success"):
            # 统一补齐前端常见字段
            result.setdefault("project_path", result.get("project_path") or result.get("output_path"))
            result.setdefault("project_format", result.get("project_format", output_format))
            result.setdefault("requested_format", output_format)
            result.setdefault("project_title", project_title)
            set_job(
                job_id,
                status="done",
                finished_at=datetime.now().isoformat(),
                result=result,
            )
        else:
            set_job(
                job_id,
                status="failed",
                finished_at=datetime.now().isoformat(),
                error=result.get("error", "工程生成失败"),
                result=result,
            )

    except Exception as e:
        logger.exception("后台工程文件任务异常")
        set_job(
            job_id,
            status="failed",
            finished_at=datetime.now().isoformat(),
            error=str(e),
        )


@app.route("/api/pipeline/project-only", methods=["POST"])
def pipeline_project_only():
    """
    仅执行工程文件生成（异步后台任务版）：
    - 需要 WAV
    - LAB / MIDI 至少提供一个
    - 支持三种上传方式：
      1) wav_file + lab_file
      2) wav_file + midi_file
      3) wav_file + notation_file（自动按扩展名识别 .lab / .mid / .midi）
    """
    try:
        # 读取工程参数
        output_format = request.form.get("format", "sv")
        project_title = request.form.get("title", "Project")
        bpm = float(request.form.get("bpm", 120))
        base_pitch = int(request.form.get("base_pitch", 60))
        f0_method = request.form.get("f0_method", "dio")
        f0_smooth = request.form.get("f0_smooth", "true").lower() == "true"
        f0_smooth_window = int(request.form.get("f0_smooth_window", 5))
        use_double_precision = request.form.get("precision", "single").lower() == "double"
        f0_floor = float(request.form.get("f0_floor", 71.0))
        f0_ceil = float(request.form.get("f0_ceil", 800.0))
        f0_device = request.form.get("f0_device", "auto")
        crepe_model = request.form.get("crepe_model", "full")
        refine_pitch = request.form.get("auto_note_pitch", "false").lower() == "true"
        export_pitch_line = request.form.get("export_pitch_line", "false").lower() == "true"
        vsqx_pitch_smooth_window = int(request.form.get("vsqx_pitch_smooth_window", 5))
        phoneme_mode = request.form.get("phoneme_mode", "none")
        lyrics_text = request.form.get("lyrics_text", "").strip()
        # 仅生成工程模式无语种上下文，固定使用日语声库；前端仍可显式覆盖
        vsqx_singer, vsqx_singer_id, vsqx_singer_bs = _select_vsqx_singer("", "project_only")
        vsqx_singer    = request.form.get("vsqx_singer",    vsqx_singer)
        vsqx_singer_id = request.form.get("vsqx_singer_id", vsqx_singer_id)
        vsqx_singer_bs = int(request.form.get("vsqx_singer_bs", vsqx_singer_bs))
        word_phoneme_map = request.form.get("word_phoneme_map", "false").lower() == "true"

        dict_source = _normalize_dict_source(request.form.get("dict_source", "default"))

        if phoneme_mode not in ("none", "merge", "hiragana", "katakana"):
            logger.warning(f"未知 phoneme_mode '{phoneme_mode}'，回退到 'none'")
            phoneme_mode = "none"

        # 兼容两种输入方式：
        # 1) 前端上传文件：wav_file（必选）+ lab_file / midi_file / notation_file（二选一）
        # 2) 已有路径：wav_path（必选）+ lab_path / midi_path（二选一）
        wav_path = request.form.get("wav_path")
        lab_path = request.form.get("lab_path") or ""
        midi_path = ""

        wav_file = request.files.get("wav_file")
        lab_file = request.files.get("lab_file")
        midi_file = request.files.get("midi_file")
        notation_file = request.files.get("notation_file")

        if wav_file is not None:
            stem, wav_path_obj, lab_path_obj = build_job_paths(wav_file.filename or "audio.wav")
            wav_file.save(str(wav_path_obj))
            wav_path = str(wav_path_obj)

            def _save_notation_as_path(storage):
                nonlocal lab_path, midi_path
                if storage is None or not storage.filename:
                    return
                filename = storage.filename.lower()
                if filename.endswith(".lab"):
                    storage.save(str(lab_path_obj))
                    lab_path = str(lab_path_obj)
                elif filename.endswith(".mid") or filename.endswith(".midi"):
                    midi_stem = sanitize_stem(storage.filename)
                    midi_stem = fit_stem_to_limit(str(WORK_DIR), f"{midi_stem}_{uuid.uuid4().hex[:6]}")
                    midi_path_obj = WORK_DIR / f"{midi_stem}.mid"
                    storage.save(str(midi_path_obj))
                    midi_path = str(midi_path_obj)
                else:
                    raise ValueError("notation_file 只支持 .lab / .mid / .midi")

            # 优先使用统一入口 notation_file；没有的话再兼容旧字段
            if notation_file is not None and notation_file.filename:
                _save_notation_as_path(notation_file)
            else:
                _save_notation_as_path(lab_file)
                _save_notation_as_path(midi_file)

        if not wav_path:
            return jsonify({"error": "请提供 wav_file 或 wav_path"}), 400

        # ── 格式校验 + 文件存在性校验 ─────────────────────────────────────
        supported_formats = pipeline.get_supported_formats().get("formats", [])
        if output_format not in supported_formats:
            return jsonify({
                "error": f"不支持的格式: {output_format}",
                "supported": supported_formats
            }), 400

        if not os.path.exists(wav_path):
            return jsonify({"error": f"WAV 文件不存在: {wav_path}"}), 400

        lab_exists = bool(lab_path and os.path.exists(lab_path))
        midi_exists = bool(midi_path and os.path.exists(midi_path))

        if not lab_exists and not midi_exists:
            return jsonify({
                "error": "请提供 LAB 文件或 MIDI 文件（至少提供其中一个）",
                "hint": "通过 lab_file / notation_file 上传 LAB，或通过 midi_file / notation_file 上传 MIDI"
            }), 400

        logger.info(
            "工程文件模式启动 (异步): format=%s wav=%s lab=%s midi=%s",
            output_format,
            wav_path,
            lab_path or "(无 LAB)",
            midi_path or "(无 MIDI)",
        )

        job_id = uuid.uuid4().hex
        set_job(
            job_id,
            status="queued",
            created_at=datetime.now().isoformat(),
        )

        Thread(
            target=run_project_only_job,
            daemon=True,
            args=(
                job_id,
                wav_path,
                lab_path or "",
                output_format,
                project_title,
                bpm,
                base_pitch,
                f0_method,
                f0_smooth,
                f0_smooth_window,
                use_double_precision,
                f0_floor,
                f0_ceil,
                refine_pitch,
                export_pitch_line,
                f0_device,
                crepe_model,
                phoneme_mode,
                midi_path,
                lyrics_text,
                vsqx_singer,
                vsqx_singer_id,
                vsqx_singer_bs,
                word_phoneme_map,
                dict_source,
                vsqx_pitch_smooth_window,
            ),
        ).start()

        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": "queued",
        }), 202

    except Exception as e:
        logger.error("工程文件生成异常: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/pipeline/f0-only", methods=["POST"])
def pipeline_f0_only():
    """仅执行 F0 提取"""
    try:
        wav_path = request.form.get("wav_path")
        method = request.form.get("method", "dio")
        f0_device = request.form.get("f0_device", "auto")
        crepe_model = request.form.get("crepe_model", "full")
        f0_floor = float(request.form.get("f0_floor", 71.0))
        f0_ceil = float(request.form.get("f0_ceil", 800.0))

        if not wav_path:
            return jsonify({"error": "缺少 wav_path"}), 400

        if method not in ["dio", "harvest", "crepe", "rmvpe"]:
            return jsonify({
                "error": f"不支持的方法: {method}",
                "supported": ["dio", "harvest", "crepe", "rmvpe"]
            }), 400

        # 验证文件存在
        if not os.path.exists(wav_path):
            return jsonify({"error": f"WAV 文件不存在: {wav_path}"}), 400

        logger.info(f"F0 提取模式启动: {method} 方法")
        result = pipeline.process_f0_only(
            wav_path,
            method=method,
            f0_floor=f0_floor,
            f0_ceil=f0_ceil,
            f0_device=f0_device,
            crepe_model=crepe_model,
        )

        if result.get("success"):
            return jsonify(result), 200

        return jsonify({
            "success": False,
            "error": "F0 提取失败",
            "processing_time": result.get("processing_time", 0)
        }), 500

    except Exception as e:
        logger.error("F0 提取模式异常: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# =====================================================================
# 对话文本框批量处理（功能 3）
# 每个对话框：左侧文本/LAB 导入，右侧音频导入；顶部"高级设置"与单文件
# 处理页面共用同一套后端对齐参数。处理结果合并写入*同一个*多音轨工程
# 文件（每个对话框对应一条独立音轨），而不是分别导出多个独立工程文件。
# =====================================================================

def run_dialogue_batch_job(job_id: str, boxes, input_mode: str = "audio", **kwargs):
    """
    对话文本框批量处理后台任务。

    input_mode == "tts" 时，boxes 里标记了 "tts" 信息（讲述人/音色/语速/
    音调/音量）但尚未提供 audio_path 的对话框，先在这里逐个处理：如果该
    框的 tts 信息里带有 preview_segments_dir（用户之前手动点过这个框的
    "生成预览"，且提交时参数未变），直接调用 tts_processor.align_segments()
    复用已合成好的分句音频，只做 Qwen3-FA 对齐；否则调用
    tts_processor.synthesize_and_align() 一次性完成合成 + 对齐。处理完成后
    回填 box["audio_path"] / box["lab_path"]，再原样复用现有的
    pipeline.process_dialogue_batch()（对它来说，这些音轨看起来就是
    "已提供 WAV + LAB"，会直接跳过对齐，进入 F0 提取 + 工程文件生成）。
    这样不需要改动 pipeline.py 里的批量处理逻辑本身。
    """
    try:
        set_job(
            job_id,
            status="running",
            started_at=datetime.now().isoformat(),
            progress={"done": 0, "total": len(boxes)},
        )

        pre_failed: list = []

        if input_mode == "tts":
            language = kwargs.get("language", "cmn")
            aligner_device = kwargs.get("aligner_device")
            english_word_align = kwargs.get("english_word_align", False)

            tts_boxes = [b for b in boxes if b.get("tts")]
            total_tts = len(tts_boxes)
            for done_tts, box in enumerate(tts_boxes, start=1):
                tts_info = box["tts"]
                # 对齐辅助移调（半音）：每个对话框独立生效，未提交时默认 0。
                box_align_pitch_shift_semitones = box.get("align_pitch_shift_semitones", 0.0)
                # 该对话框的"单独设置"：TTS跟读固定使用 Qwen3-ForcedAligner
                # （不支持覆盖 aligner_backend），但语言 / 英语单词级对齐仍
                # 可按框覆盖，与"音频跟读"模式语义一致。
                box_override = box.get("override") or {}
                box_language = box_override.get("language", language)
                box_english_word_align = box_override.get("english_word_align", english_word_align)
                set_job(
                    job_id, status="running",
                    progress={"done": 0, "total": len(boxes)},
                    tts_progress={"done": done_tts - 1, "total": total_tts},
                )
                stem = f"dlg_tts_{box.get('index', 0):03d}_{uuid.uuid4().hex[:6]}"

                if tts_info.get("preview_segments_dir") and tts_info.get("preview_sentences") and tts_info.get("preview_wav_path"):
                    # 这一框之前手动点过"生成预览"，分句音频已经在磁盘上——
                    # 只需要对齐，不重新调用 TTS 引擎合成一遍。
                    preview_segments_dir = tts_info["preview_segments_dir"]
                    align_result = tts_processor.align_segments(
                        segments_dir=preview_segments_dir,
                        sentences=tts_info["preview_sentences"],
                        language=box_language,
                        aligner_device=aligner_device,
                        english_word_align=box_english_word_align,
                        align_pitch_shift_semitones=box_align_pitch_shift_semitones,
                    )
                    shutil.rmtree(preview_segments_dir, ignore_errors=True)

                    if align_result.get("success"):
                        final_wav_path = str(WORK_DIR / f"{stem}.wav")
                        shutil.move(tts_info["preview_wav_path"], final_wav_path)
                        final_lab_path = str(WORK_DIR / f"{stem}.lab")
                        Path(final_lab_path).write_text(align_result["lab_content"], encoding="utf-8")
                        tts_result = {
                            "success": True,
                            "wav_path": final_wav_path,
                            "lab_path": final_lab_path,
                            "lab_content": align_result["lab_content"],
                            "audio_duration": tts_processor.get_wav_duration_100ns(final_wav_path),
                            "sentence_count": align_result["sentence_count"],
                            "warnings": align_result["warnings"],
                        }
                    else:
                        Path(tts_info["preview_wav_path"]).unlink(missing_ok=True)
                        tts_result = align_result
                else:
                    # 没有先手动预览（或预览已过期）：先合成分句音频，再
                    # 整体交给 Qwen3-FA 对齐，一次做完。
                    tts_result = tts_processor.synthesize_and_align(
                        text=tts_info.get("text", ""), language=box_language,
                        voice=tts_info.get("voice", ""),
                        engine=tts_info.get("engine", "") or tts_processor.DEFAULT_ENGINE,
                        work_dir=str(WORK_DIR), stem=stem,
                        rate=tts_info.get("rate", "+0%"),
                        volume=tts_info.get("volume", "+0%"),
                        pitch=tts_info.get("pitch", "+0Hz"),
                        aligner_device=aligner_device,
                        english_word_align=box_english_word_align,
                        align_pitch_shift_semitones=box_align_pitch_shift_semitones,
                    )

                if tts_result.get("success"):
                    box["audio_path"] = tts_result["wav_path"]
                    # 若该对话框本身已手动提供了 LAB / MIDI（TTS 模式下较少见，
                    # 但保留兼容：例如想用 TTS 音频套用已有的节奏标注），
                    # 优先保留用户提供的 LAB / MIDI，不用 TTS 自动对齐结果覆盖。
                    if not box.get("lab_path") and not box.get("midi_path"):
                        box["lab_path"] = tts_result["lab_path"]
                else:
                    box["_tts_error"] = tts_result.get("error", "TTS 合成/对齐失败")

            set_job(
                job_id, status="running",
                progress={"done": 0, "total": len(boxes)},
                tts_progress={"done": total_tts, "total": total_tts},
            )

            # TTS 合成/对齐失败的框直接标记为 failed，不再进入
            # process_dialogue_batch（它没有音频可用，会被当成"未提供音频"
            # 静默跳过，丢失具体失败原因，这里提前拦下来显式报错）。
            for box in tts_boxes:
                if not box.get("audio_path"):
                    pre_failed.append({
                        "index": box.get("index"),
                        "status": "failed",
                        "error": box.get("_tts_error", "TTS 合成/对齐失败"),
                    })
            boxes = [b for b in boxes if not (b.get("tts") and not b.get("audio_path"))]

        def _progress_cb(done, total, box_result):
            set_job(
                job_id,
                status="running",
                progress={"done": done, "total": total},
                last_box=box_result,
            )

        result = pipeline.process_dialogue_batch(boxes, progress_cb=_progress_cb, **kwargs)

        if pre_failed:
            result["boxes"] = pre_failed + result.get("boxes", [])
            result["failed_count"] = result.get("failed_count", 0) + len(pre_failed)

        if result.get("success"):
            set_job(
                job_id,
                status="done",
                finished_at=datetime.now().isoformat(),
                result=result,
            )
        else:
            set_job(
                job_id,
                status="failed",
                finished_at=datetime.now().isoformat(),
                error=result.get("error"),
                result=result,
            )

    except Exception as e:
        logger.exception("对话文本框批量处理后台任务异常")
        set_job(
            job_id,
            status="failed",
            finished_at=datetime.now().isoformat(),
            error=str(e),
        )


@app.route("/api/dialogue/process", methods=["POST"])
def dialogue_process():
    """
    对话文本框批量处理（异步后台任务版，与其他 pipeline 路由共用
    /api/pipeline/job/<job_id> 轮询进度）。

    请求为 multipart/form-data：
      - input_mode      : "audio"（默认，沿用原有"音频跟读"流程）或
                          "tts"（"TTS跟读"：不提供 audio_{i}，改为提供
                          tts_text_{i} / tts_voice_{i} / tts_rate_{i} /
                          tts_pitch_{i} / tts_volume_{i}，由后端调用
                          EdgeTTS 合成 + Qwen3-FA 对齐生成音频与 LAB）
      - box_count       : 对话框总数
      - text_{i}        : 第 i 个对话框的台词文本（可选；tts 模式下缺省时回退使用 tts_text_{i}）
      - audio_{i}       : 第 i 个对话框的音频文件（input_mode=audio 时必需之一；
                          缺失则该对话框跳过；每框限 1 个音频）
      - tts_text_{i}    : 第 i 个对话框的台词文本（input_mode=tts 时必填）
      - tts_voice_{i}   : 第 i 个对话框使用的 EdgeTTS 音色 ShortName（input_mode=tts 时必填）
      - tts_rate_{i} / tts_pitch_{i} / tts_volume_{i} : 语速/音调/音量（可选，默认 +0%/+0Hz/+0%）
      - tts_preview_id_{i} : 该框手动点击"生成预览"（/api/tts/synthesize_preview）
                          后返回的缓存凭证（可选）。若提交的合成参数
                          （text/engine/voice/rate/pitch/volume/language）
                          与预览时完全一致，后台任务会直接复用预览阶段
                          已合成好的分句音频去对齐，不重新合成；参数
                          对不上或缺省时，退回"先合成再对齐"的完整流程。
      - lab_{i}         : 第 i 个对话框的 LAB 标注文件（可选；提供时跳过对齐，
                          直接使用该 LAB 生成对应音轨——最高优先级）
      - mid_{i}         : 第 i 个对话框的 MIDI 文件（可选；无 lab_{i} 时提供则跳过对齐，
                          从 MIDI 音符自动生成段落 + 读取 BPM/音高；.mid/.midi）
      - notation_{i}    : 统一入口，等价于 lab_{i} / mid_{i} 之一（按扩展名自动识别，
                          与 /api/pipeline/project-only 的 notation_file 语义一致）；
                          若同时提供 lab_{i}/mid_{i}，以它们为准。
      - processing_mode : "full"（默认，完整处理：无 LAB/MIDI 的框走对齐）或
                          "project-only"（仅生成工程：跳过对齐，无 LAB/MIDI 的框直接跳过）。
                          input_mode="tts" 时强制视为 "full"（TTS 音频当场合成，
                          不存在"仅生成工程"这种依赖已有音频的场景）。
      - phoneme_mode    : "none"（默认）/"merge"/"hiragana"/"katakana"，仅对来自
                          LAB 的段落、且输出格式非 USTX 时生效
      - format          : "sv" / "vsqx" / "ustx"（USTX 原生支持多音轨）
      - 其余参数与 /api/pipeline/full 基本一致（language / title / bpm / f0_* /
        aligner_backend / word_phoneme_map / dict_source 等），对全部对话框统一生效。
        input_mode="tts" 时对齐固定使用 Qwen3-ForcedAligner，aligner_backend
        字段会被忽略。
      - override_enabled_{i}  : 第 i 个对话框是否开启"单独设置"（"true"/"false"，
                          默认 "false"）。开启时，下列 override_* 字段会覆盖该框
                          自己的对齐后端/语言/词典/音素转换/F0 等设置，其余对话框
                          不受影响，仍使用上面整批统一的全局参数。不支持覆盖 bpm
                          （BPM 决定整批对话框合并后的时间轴换算，必须全局统一）。
      - override_aligner_backend_{i} / override_language_{i} /
        override_english_word_align_{i} / override_word_phoneme_map_{i} /
        override_phoneme_mode_{i} / override_dict_source_{i} /
        override_base_pitch_{i} / override_auto_note_pitch_{i} /
        override_export_pitch_line_{i} / override_f0_method_{i} /
        override_f0_device_{i} / override_crepe_model_{i} / override_precision_{i} /
        override_f0_smooth_{i} / override_f0_smooth_window_{i} /
        override_vsqx_pitch_smooth_window_{i} / override_f0_floor_{i} /
        override_f0_ceil_{i} : 仅在 override_enabled_{i}="true" 时读取，字段语义
                          与同名的整批全局参数一致。
    """
    try:
        box_count = int(request.form.get("box_count", 0))
        if box_count <= 0:
            return jsonify({"error": "box_count 必须大于 0"}), 400
        if box_count > 64:
            return jsonify({"error": "对话框数量过多（上限 64）"}), 400

        input_mode = request.form.get("input_mode", "audio")
        if input_mode not in ("audio", "tts"):
            input_mode = "audio"

        language = request.form.get("language", "cmn")
        output_format = request.form.get("format", "sv")
        if output_format not in ("sv", "vsqx", "ustx", "utau"):
            return jsonify({
                "error": f"对话文本框批量处理暂不支持输出格式: {output_format}（仅支持 sv / vsqx / ustx，多音轨概念不适用于 MIDI 标准文件）"
            }), 400
        if output_format == "utau":
            output_format = "ustx"

        processing_mode = request.form.get("processing_mode", "full")
        if processing_mode not in ("full", "project-only"):
            processing_mode = "full"
        if input_mode == "tts":
            # TTS跟读的音频是当场合成的，不存在"仅生成工程（复用已有音频）"
            # 这个概念，统一按完整处理走（对齐 + F0 + 工程文件）。
            processing_mode = "full"

        phoneme_mode = request.form.get("phoneme_mode", "none")
        if phoneme_mode not in ("none", "merge", "hiragana", "katakana"):
            logger.warning(f"未知 phoneme_mode '{phoneme_mode}'，回退到 'none'")
            phoneme_mode = "none"

        project_title = request.form.get("title", "Dialogue Project")

        bpm = float(request.form.get("bpm", 120))
        base_pitch = int(request.form.get("base_pitch", 60))
        f0_method = request.form.get("f0_method", "dio")
        f0_smooth = request.form.get("f0_smooth", "true").lower() == "true"
        f0_device = request.form.get("f0_device", "auto")
        crepe_model = request.form.get("crepe_model", "full")
        f0_smooth_window = int(request.form.get("f0_smooth_window", 5))
        use_double_precision = request.form.get("precision", "single").lower() == "double"
        f0_floor = float(request.form.get("f0_floor", 71.0))
        f0_ceil = float(request.form.get("f0_ceil", 800.0))
        refine_pitch = request.form.get("auto_note_pitch", "false").lower() == "true"
        export_pitch_line = request.form.get("export_pitch_line", "true").lower() == "true"
        vsqx_pitch_smooth_window = int(request.form.get("vsqx_pitch_smooth_window", 5))

        aligner_backend = request.form.get("aligner_backend", "mfa")
        if aligner_backend not in ("mfa", "whisperx", "qwen3_asr", "qwen3_aligner", "nemo_aligner"):
            aligner_backend = "mfa"

        # 对齐工具（WhisperX/Qwen3/NeMo）运行设备，与 f0_device（F0 提取设备）解耦。
        # 未提交该字段时传 None，让 pipeline._run_alignment 按约定回退到 f0_device。
        aligner_device = request.form.get("aligner_device", "").strip() or None

        whisperx_model = request.form.get("whisperx_model", "large-v3")
        if whisperx_model not in ("large-v3", "large-v3-turbo", "large-v2", "medium", "small", "base", "tiny"):
            whisperx_model = "large-v3"

        try:
            whisperx_batch_size = int(request.form.get("whisperx_batch_size", 16))
        except (TypeError, ValueError):
            whisperx_batch_size = 16
        whisperx_batch_size = max(1, min(whisperx_batch_size, 64))

        nemo_model = request.form.get("nemo_model", "").strip()
        english_word_align = request.form.get("english_word_align", "false").lower() == "true"
        word_phoneme_map   = request.form.get("word_phoneme_map",   "false").lower() == "true"

        # 对齐辅助移调（半音）：现为每个对话框独立生效，见下方
        # "逐个对话框保存音频 / LAB / MIDI 文件" 循环里的 align_pitch_shift_{i}
        # 解析（音频跟读走 pipeline._run_alignment，TTS跟读走
        # tts_processor.align_segments / synthesize_and_align，均已支持
        # 按每个对话框传入各自的移调值；仅影响送入对齐后端的临时音频
        # 副本，不影响最终 WAV / F0 / 工程文件音高）。

        # USTX 没有 phonemes 字段，词典/单词映射对其无意义；即便前端已隐藏
        # 相关控件，这里仍显式兜底关闭，防止残留表单值被误提交生效。
        if output_format == "ustx":
            word_phoneme_map = False

        # 【解耦】前端不再要求用户手动开启"英语单词级对齐"开关才能使用
        # "单词映射音素"/词典来源功能——只要用户开启了 word_phoneme_map，
        # 就在后端自动补上 english_word_align=True（日语除外，前端本就不
        # 提供该开关），确保对齐阶段产出整词级 LAB，词典查词才有意义；
        # 用户仍可手动开启 english_word_align 以获得其独立效果（英语单词
        # 直接输出而不拆分为 ARPABET），两者互不冲突，取或即可。
        if word_phoneme_map and language != "jpn":
            english_word_align = True

        dict_source = _normalize_dict_source(request.form.get("dict_source", "default"))

        vsqx_singer    = request.form.get("vsqx_singer",    "MIKU_V4_Chinese")
        vsqx_singer_id = request.form.get("vsqx_singer_id", "BNGE7CP7EMTRSNC3")
        vsqx_singer_bs = int(request.form.get("vsqx_singer_bs", 4))
        if output_format == "vsqx":
            # 【修复】此前不论 processing_mode 一律按 "full" 模式选声库，
            # 导致"仅生成工程"(project-only) 模式下也被按语种选中文声库，
            # 而不是与单文件仅生成工程页面一致的日语声库。现在按
            # processing_mode 区分：project-only → 固定日语声库（忽略语种，
            # 与 _select_vsqx_singer("", "project_only") 语义一致）；
            # full → 按语种选择（en/ja/ko 各自声库，cmn/yue 等回退中文）。
            singer_mode = "project_only" if processing_mode == "project-only" else "full"
            vsqx_singer, vsqx_singer_id, vsqx_singer_bs = _select_vsqx_singer(language, singer_mode)

        # ── 逐个对话框保存音频 / LAB / MIDI 文件到工作目录 ─────────────────
        boxes = []
        for i in range(box_count):
            text = request.form.get(f"text_{i}", "").strip()
            audio_file = request.files.get(f"audio_{i}")
            lab_file = request.files.get(f"lab_{i}")
            mid_file = request.files.get(f"mid_{i}")
            notation_file = request.files.get(f"notation_{i}")
            # 对齐辅助移调（半音）：每个对话框独立提交，未提交时（如旧版
            # 前端缓存）默认为 0，与该功能上线前行为一致。
            box_align_pitch_shift_semitones = _parse_align_pitch_shift(
                request.form.get(f"align_pitch_shift_{i}", 0)
            )

            # 该对话框的"单独设置"覆盖值（对齐后端/语言/英语单词级对齐/
            # 词典/音素转换/高级设置，不含 BPM）；未开启时为 None。提前到
            # 这里解析（而不是等到循环末尾）是因为下面 TTS 预览复用校验
            # 需要该框的有效语言（box_override 里的 language，未开启覆盖
            # 时才回退到整批统一的 language）来判断能否复用已生成的预览。
            box_override = _parse_box_override(request.form, i)
            box_effective_language = (box_override or {}).get("language", language)

            audio_path = None
            lab_path = None
            midi_path = None
            tts_info = None

            if input_mode == "tts":
                tts_text = request.form.get(f"tts_text_{i}", "").strip() or text
                tts_voice = request.form.get(f"tts_voice_{i}", "").strip()
                if tts_text and tts_voice:
                    tts_engine_i = request.form.get(f"tts_engine_{i}", "").strip() or tts_processor.DEFAULT_ENGINE
                    tts_rate_i = request.form.get(f"tts_rate_{i}", "+0%")
                    tts_pitch_i = request.form.get(f"tts_pitch_{i}", "+0Hz")
                    tts_volume_i = request.form.get(f"tts_volume_{i}", "+0%")
                    tts_info = {
                        "text": tts_text,
                        "voice": tts_voice,
                        "engine": tts_engine_i,
                        "rate": tts_rate_i,
                        "pitch": tts_pitch_i,
                        "volume": tts_volume_i,
                    }
                    text = tts_text

                    # 若前端带上了这一框的 tts_preview_id_{i}（用户点过这个
                    # 框的"生成预览"），且这次提交的合成参数与预览时完全
                    # 一致，直接复用预览阶段已经生成好的分句音频，后台
                    # 任务对这一框只需要做对齐；否则（没点过预览 / 点完
                    # 预览后又改了文本或参数）退回"先合成再对齐"的完整
                    # 流程——与 /api/tts/process 的单文件复用逻辑一致。
                    # 注意：这里用 box_effective_language（该框自己的有效
                    # 语言）而不是整批统一的 language 做匹配——若该框单独
                    # 设置了不同的语言，用全局 language 比对会永远不匹配，
                    # 导致预览被误判为"参数已变化"而白白重新合成一遍。
                    box_preview_id = request.form.get(f"tts_preview_id_{i}", "").strip()
                    box_preview_entry = _tts_preview_take(box_preview_id, {
                        "text": tts_text, "engine": tts_engine_i, "voice": tts_voice,
                        "rate": tts_rate_i, "volume": tts_volume_i, "pitch": tts_pitch_i,
                        "language": box_effective_language,
                    }) if box_preview_id else None
                    if box_preview_entry:
                        tts_info["preview_segments_dir"] = box_preview_entry["segments_dir"]
                        tts_info["preview_sentences"] = box_preview_entry["sentences"]
                        tts_info["preview_wav_path"] = box_preview_entry["wav_path"]
                # TTS 模式下极少见但仍保留兼容：若该框另外手动提供了 LAB/MIDI，
                # 走下面同一套 _save_lab / _save_midi 保存逻辑，随后在
                # run_dialogue_batch_job 里会优先沿用用户提供的 LAB/MIDI，
                # 不用 TTS 自动对齐结果覆盖。
            elif audio_file is not None and audio_file.filename:
                _stem, wav_path_obj, _lab_path_obj = build_job_paths(
                    audio_file.filename or f"dialogue_{i}.wav"
                )
                audio_file.save(str(wav_path_obj))
                audio_path = str(wav_path_obj)

            def _save_lab(storage):
                nonlocal lab_path
                lab_stem = sanitize_stem(storage.filename)
                lab_stem = fit_stem_to_limit(str(WORK_DIR), f"{lab_stem}_{uuid.uuid4().hex[:6]}")
                lab_path_obj = WORK_DIR / f"{lab_stem}.lab"
                storage.save(str(lab_path_obj))
                lab_path = str(lab_path_obj)

            def _save_midi(storage):
                nonlocal midi_path
                midi_stem = sanitize_stem(storage.filename)
                midi_stem = fit_stem_to_limit(str(WORK_DIR), f"{midi_stem}_{uuid.uuid4().hex[:6]}")
                midi_path_obj = WORK_DIR / f"{midi_stem}.mid"
                storage.save(str(midi_path_obj))
                midi_path = str(midi_path_obj)

            # 统一入口 notation_{i}：按扩展名自动识别为 LAB 或 MIDI
            if notation_file is not None and notation_file.filename:
                fname = notation_file.filename.lower()
                if fname.endswith(".lab") or fname.endswith(".txt"):
                    _save_lab(notation_file)
                elif fname.endswith(".mid") or fname.endswith(".midi"):
                    _save_midi(notation_file)

            # 显式字段 lab_{i} / mid_{i}：优先于 notation_{i} 的识别结果
            if lab_file is not None and lab_file.filename:
                _save_lab(lab_file)
            if mid_file is not None and mid_file.filename:
                _save_midi(mid_file)

            # 该对话框的"单独设置"覆盖值已在本次循环开头解析好（box_override /
            # box_effective_language），此处直接复用，避免重复解析同一批表单字段。

            boxes.append({
                "index": i,
                "text": text,
                "audio_path": audio_path,
                "lab_path": lab_path,
                "midi_path": midi_path,
                "tts": tts_info,
                "align_pitch_shift_semitones": box_align_pitch_shift_semitones,
                "override": box_override,
            })

        if input_mode == "tts":
            if not any(b["tts"] for b in boxes):
                return jsonify({"error": "请至少为一个对话框输入台词文本并选择音色后再开始处理"}), 400
        elif not any(b["audio_path"] for b in boxes):
            return jsonify({"error": "请至少为一个对话框提供音频文件后再开始处理"}), 400

        if processing_mode == "project-only" and not any(
            b["audio_path"] and (b["lab_path"] or b["midi_path"]) for b in boxes
        ):
            return jsonify({
                "error": "「仅生成工程」模式下，请至少为一个对话框同时提供音频与 LAB / MIDI 文件"
            }), 400

        job_id = uuid.uuid4().hex
        set_job(
            job_id,
            status="queued",
            created_at=datetime.now().isoformat(),
        )

        logger.info(
            f"对话文本框批量处理启动 (input_mode={input_mode}, mode={processing_mode}, "
            f"backend={aligner_backend}, format={output_format}, boxes={box_count})，"
            f"投递后台任务: {job_id}"
        )

        Thread(
            target=run_dialogue_batch_job,
            daemon=True,
            kwargs=dict(
                job_id=job_id,
                boxes=boxes,
                input_mode=input_mode,
                language=language,
                output_format=output_format,
                project_title=project_title,
                bpm=bpm,
                base_pitch=base_pitch,
                f0_method=f0_method,
                f0_smooth=f0_smooth,
                f0_smooth_window=f0_smooth_window,
                use_double_precision=use_double_precision,
                f0_floor=f0_floor,
                f0_ceil=f0_ceil,
                refine_pitch=refine_pitch,
                export_pitch_line=export_pitch_line,
                vsqx_pitch_smooth_window=vsqx_pitch_smooth_window,
                f0_device=f0_device,
                crepe_model=crepe_model,
                aligner_backend=aligner_backend,
                aligner_device=aligner_device,
                whisperx_model=whisperx_model,
                whisperx_batch_size=whisperx_batch_size,
                nemo_model=(nemo_model or None),
                processing_mode=processing_mode,
                phoneme_mode=phoneme_mode,
                english_word_align=english_word_align,
                vsqx_singer=vsqx_singer,
                vsqx_singer_id=vsqx_singer_id,
                vsqx_singer_bs=vsqx_singer_bs,
                word_phoneme_map=word_phoneme_map,
                dict_source=dict_source,
            ),
        ).start()

        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": "queued",
            "box_count": box_count,
        }), 202

    except Exception as e:
        logger.exception("对话文本框批量处理启动失败")
        return jsonify({"error": str(e)}), 500


# =====================================================================
# TTS跟读（讲述人 + EdgeTTS）
#   与其它对齐后端不同：TTS跟读不需要用户上传音频——文本本身就是"标注
#   来源"，音频由 EdgeTTS 合成。整体思路：
#     文本 → 按句切分 → EdgeTTS 逐句合成 → Qwen3-FA 逐句对齐 → 按偏移
#     拼接成完整 WAV + LAB（见 tts_processor.synthesize_and_align）→
#     （完整处理模式下）直接复用现成的 pipeline.process_project_only()
#     做 F0 提取 + 工程文件生成，不需要在 pipeline.py 里另写一套流程。
# =====================================================================

@app.route("/api/tts/engines", methods=["GET"])
def tts_engines():
    """
    获取全部已注册 TTS 引擎（讲述人 / EdgeTTS / 后续可扩展）及其可用性，
    供前端渲染"选择 TTS"下拉框。
    """
    try:
        return jsonify({"success": True, "engines": tts_processor.list_engines()}), 200
    except Exception as e:
        logger.error(f"获取 TTS 引擎列表失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/tts/status", methods=["GET"])
def tts_status():
    """检查指定 TTS 引擎是否已安装可用。?engine= 不传时默认检查 EdgeTTS
    （向后兼容旧前端）。"""
    engine = request.args.get("engine", "").strip() or tts_processor.DEFAULT_ENGINE
    ok, msg = tts_processor.check_available(engine)
    return jsonify({"success": True, "available": ok, "message": msg}), 200


@app.route("/api/tts/voices", methods=["GET"])
def tts_voices():
    """
    获取指定 TTS 引擎的音色列表。?engine= 指定引擎（默认 EdgeTTS，向后兼容
    旧前端）；?language= 传内部语言短代码（cmn/eng/jpn/kor/yue 等）时按语
    种大类过滤，不传则返回全部音色。
    """
    try:
        engine = request.args.get("engine", "").strip() or tts_processor.DEFAULT_ENGINE
        language = request.args.get("language", "").strip() or None
        voices = tts_processor.list_voices(engine, language)
        return jsonify({"success": True, "voices": voices}), 200
    except ImportError as e:
        return jsonify({"success": False, "error": f"引擎依赖未安装: {e}"}), 500
    except Exception as e:
        logger.error(f"获取 TTS 音色列表失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/tts/narrators", methods=["GET"])
def tts_narrators_list():
    """获取全部"讲述人"档案（名字 + 音色 + 语速/音调/音量）"""
    try:
        return jsonify({"success": True, "narrators": tts_processor.list_narrators()}), 200
    except Exception as e:
        logger.error(f"获取讲述人列表失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/tts/narrators", methods=["POST"])
def tts_narrators_upsert():
    """新建 / 更新一个讲述人档案。body: {id?, name, voice, rate, pitch, volume, language}"""
    try:
        payload = request.get_json(force=True, silent=True) or {}
        record = tts_processor.upsert_narrator(payload)
        return jsonify({"success": True, "narrator": record}), 200
    except Exception as e:
        logger.error(f"保存讲述人失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/tts/narrators/<narrator_id>", methods=["DELETE"])
def tts_narrators_delete(narrator_id: str):
    """删除一个讲述人档案"""
    try:
        ok = tts_processor.delete_narrator(narrator_id)
        if not ok:
            return jsonify({"success": False, "error": "讲述人不存在"}), 404
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.error(f"删除讲述人失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/tts/preview", methods=["POST"])
def tts_preview():
    """
    试听预览：只合成一小段音频（不切句、不对齐），直接返回音频字节流，
    前端用 <audio> 播放即可，不落盘到工作目录。
    body: {text, engine?, voice, rate?, pitch?, volume?}
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        engine = payload.get("engine", "").strip() or tts_processor.DEFAULT_ENGINE
        audio_bytes = tts_processor.synthesize_preview(
            engine=engine,
            text=payload.get("text", ""),
            voice=payload.get("voice", ""),
            rate=payload.get("rate", "+0%"),
            volume=payload.get("volume", "+0%"),
            pitch=payload.get("pitch", "+0Hz"),
        )
        mimetype = "audio/wav" if engine == "windows_sapi" else "audio/mpeg"
        return Response(audio_bytes, mimetype=mimetype)
    except (ValueError, RuntimeError) as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"TTS 预览失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/tts/synthesize_preview", methods=["POST"])
def tts_synthesize_preview():
    """
    手动分段预览：由前端"生成预览"按钮触发（不再随输入自动防抖触发）。
    输入文本切分（优先按换行分段，单行过长时再按长度区间在句号/逗号处二次
    切割，规则见 tts_processor.split_sentences()）→ 每句用所选 TTS 引擎
    合成 → 按偏移拼接成完整音频返回给前端做即时试听。这一步只做 TTS 合成，
    不调用 Qwen3-FA 对齐——对齐推迟到用户点击"开始处理"时才做（见
    /api/tts/process），避免"只是想听听音色/语速"也要跑一次对齐。

    不再有句子数量上限：预览会合成完整输入文本（不再只取前 N 句）。

    合成产物（分句音频 + 拼接后的完整音频）会保留在磁盘上并按 preview_id
    缓存，如果用户紧接着点击"开始处理"且没有改动文本/引擎/音色/语速/
    音调/音量/语种，/api/tts/process 会直接复用这份音频去对齐，不会重新
    合成一遍；如果参数对不上（或用户压根没点过预览），则退回"先合成再
    对齐"的完整流程。

    body(JSON): {text, language?, engine?, voice, rate?, pitch?, volume?}
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        text = (payload.get("text") or "").strip()
        if not text:
            return jsonify({"success": False, "error": "文本为空"}), 400

        voice = (payload.get("voice") or "").strip()
        if not voice:
            return jsonify({"success": False, "error": "未选择音色"}), 400

        engine = payload.get("engine", "").strip() or tts_processor.DEFAULT_ENGINE
        language = payload.get("language", "cmn")
        rate = payload.get("rate", "+0%")
        volume = payload.get("volume", "+0%")
        pitch = payload.get("pitch", "+0Hz")

        preview_dir = WORK_DIR / "_tts_preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        stem = f"preview_{uuid.uuid4().hex[:10]}"

        result = tts_processor.synthesize_segments_only(
            text=text, language=language, voice=voice, engine=engine,
            work_dir=str(preview_dir), stem=stem,
            rate=rate, volume=volume, pitch=pitch,
        )

        if not result.get("success"):
            return jsonify({"success": False, "error": result.get("error", "预览生成失败")}), 400

        wav_path = Path(result["wav_path"])
        try:
            audio_b64 = base64.b64encode(wav_path.read_bytes()).decode("ascii")
        except Exception:
            shutil.rmtree(result["segments_dir"], ignore_errors=True)
            wav_path.unlink(missing_ok=True)
            raise

        preview_id = _tts_preview_store({
            "segments_dir": result["segments_dir"],
            "sentences": result["sentences"],
            "wav_path": str(wav_path),
            "created_at": datetime.now().isoformat(),
            # 复用校验用：/api/tts/process 提交的这几项必须与预览时完全
            # 一致，否则视为预览已过期。
            "text": text, "engine": engine, "voice": voice,
            "rate": rate, "volume": volume, "pitch": pitch, "language": language,
        })

        return jsonify({
            "success": True,
            "preview_id": preview_id,
            "audio_base64": audio_b64,
            "sentence_count": result.get("sentence_count", 0),
            "audio_duration": result.get("audio_duration", 0),
            "warnings": result.get("warnings", []),
        }), 200

    except Exception as e:
        logger.error(f"TTS 分段预览失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


def run_tts_pipeline_job(
    job_id: str,
    text: str,
    language: str,
    voice: str,
    rate: str,
    volume: str,
    pitch: str,
    processing_mode: str,          # "mfa-only"（仅标注）或 "full"（完整处理）
    engine: str,                   # 使用哪个 TTS 引擎（讲述人 / EdgeTTS / ...）
    output_format: str,
    project_title: str,
    bpm: float,
    base_pitch: int,
    f0_method: str,
    f0_smooth: bool,
    f0_smooth_window: int,
    use_double_precision: bool,
    f0_floor: float,
    f0_ceil: float,
    refine_pitch: bool,
    export_pitch_line: bool,
    vsqx_pitch_smooth_window: int,
    f0_device: str,
    crepe_model: str,
    aligner_device: str,
    english_word_align: bool,
    vsqx_singer: str,
    vsqx_singer_id: str,
    vsqx_singer_bs: int,
    word_phoneme_map: bool,
    dict_source: str,
    align_pitch_shift_semitones: float = 0.0,
    preview_segments_dir: Optional[str] = None,
    preview_sentences: Optional[list] = None,
    preview_wav_path: Optional[str] = None,
):
    try:
        set_job(job_id, status="running", started_at=datetime.now().isoformat(),
                progress={"done": 0, "total": 0})

        def _progress_cb(done, total):
            set_job(job_id, status="running", progress={"done": done, "total": total})

        stem = f"tts_{uuid.uuid4().hex[:10]}"

        if preview_segments_dir and preview_sentences and preview_wav_path:
            # 用户已经先手动点过"生成预览"，分句音频已经在磁盘上——这里只
            # 需要对齐，不重新调用 TTS 引擎合成一遍（对应"将现有的分割
            # 音频交给 QWEN3-FA 对齐"这条路径）。
            align_result = tts_processor.align_segments(
                segments_dir=preview_segments_dir, sentences=preview_sentences,
                language=language, aligner_device=aligner_device,
                english_word_align=english_word_align,
                align_pitch_shift_semitones=align_pitch_shift_semitones,
                progress_cb=_progress_cb,
            )
            shutil.rmtree(preview_segments_dir, ignore_errors=True)

            if not align_result.get("success"):
                Path(preview_wav_path).unlink(missing_ok=True)
                set_job(
                    job_id, status="failed", finished_at=datetime.now().isoformat(),
                    error=align_result.get("error", "Qwen3-FA 对齐失败"), result=align_result,
                )
                return

            final_wav_path = str(WORK_DIR / f"{stem}.wav")
            shutil.move(preview_wav_path, final_wav_path)
            final_lab_path = str(WORK_DIR / f"{stem}.lab")
            Path(final_lab_path).write_text(align_result["lab_content"], encoding="utf-8")

            tts_result = {
                "success": True,
                "wav_path": final_wav_path,
                "lab_path": final_lab_path,
                "lab_content": align_result["lab_content"],
                "audio_duration": tts_processor.get_wav_duration_100ns(final_wav_path),
                "sentence_count": align_result["sentence_count"],
                "warnings": align_result["warnings"],
            }
        else:
            # 没有先手动预览（或预览已过期）：先合成分句音频，再整体交给
            # Qwen3-FA 对齐，一次做完。
            tts_result = tts_processor.synthesize_and_align(
                text=text, language=language, voice=voice, engine=engine,
                work_dir=str(WORK_DIR), stem=stem,
                rate=rate, volume=volume, pitch=pitch,
                aligner_device=aligner_device, english_word_align=english_word_align,
                align_pitch_shift_semitones=align_pitch_shift_semitones,
                progress_cb=_progress_cb,
            )

        if not tts_result.get("success"):
            set_job(
                job_id, status="failed", finished_at=datetime.now().isoformat(),
                error=tts_result.get("error", "TTS 合成/对齐失败"), result=tts_result,
            )
            return

        if processing_mode == "mfa-only":
            result = {**tts_result, "processing_time": 0, "aligner_backend": "qwen3_aligner"}
            set_job(job_id, status="done", finished_at=datetime.now().isoformat(), result=result)
            return

        project_result = pipeline.process_project_only(
            wav_path=tts_result["wav_path"],
            lab_path=tts_result["lab_path"],
            output_format=output_format,
            project_title=project_title,
            bpm=bpm, base_pitch=base_pitch,
            f0_method=f0_method, f0_smooth=f0_smooth,
            f0_smooth_window=f0_smooth_window,
            use_double_precision=use_double_precision,
            f0_floor=f0_floor, f0_ceil=f0_ceil,
            refine_pitch=refine_pitch, export_pitch_line=export_pitch_line,
            vsqx_pitch_smooth_window=vsqx_pitch_smooth_window,
            f0_device=f0_device, crepe_model=crepe_model,
            vsqx_singer=vsqx_singer, vsqx_singer_id=vsqx_singer_id,
            vsqx_singer_bs=vsqx_singer_bs,
            word_phoneme_map=word_phoneme_map,
            language=language, original_text=text, dict_source=dict_source,
        )

        merged_result = {
            **project_result,
            "lab_content": tts_result["lab_content"],
            "wav_path": tts_result["wav_path"],
            "sentence_count": tts_result.get("sentence_count"),
            "warnings": tts_result.get("warnings"),
            "aligner_backend": "qwen3_aligner",
        }

        if project_result.get("success"):
            set_job(job_id, status="done", finished_at=datetime.now().isoformat(), result=merged_result)
        else:
            set_job(
                job_id, status="failed", finished_at=datetime.now().isoformat(),
                error=project_result.get("error"), result=merged_result,
            )

    except Exception as e:
        logger.exception("TTS 后台任务异常")
        set_job(job_id, status="failed", finished_at=datetime.now().isoformat(), error=str(e))


@app.route("/api/tts/process", methods=["POST"])
def tts_process():
    """
    TTS跟读（讲述人 + EdgeTTS）单文件处理入口（异步后台任务版，与其它
    pipeline 路由共用 /api/pipeline/job/<job_id> 轮询进度）。

    请求为 application/x-www-form-urlencoded 或 multipart/form-data：
      - text              : 全文本（必填，将优先按换行自动分段，单行过长时再按句号/逗号二次切割）
      - voice             : EdgeTTS 音色 ShortName（必填，如 zh-CN-XiaoxiaoNeural）
      - rate / pitch / volume : 语速 / 音调 / 音量，格式分别为 "+N%"/"+NHz"/"+N%"
      - language          : 语种短代码，默认 cmn
      - processing_mode   : "mfa-only"（仅标注，输出 WAV+LAB）或
                            "full"（默认，完整处理：标注 + F0 + 工程文件）
      - 其余参数（format / title / bpm / f0_* / word_phoneme_map / dict_source 等）
        与 /api/pipeline/full 含义一致
    """
    try:
        payload = request.form

        text = payload.get("text", "").strip()
        if not text:
            return jsonify({"error": "缺少 text"}), 400

        voice = payload.get("voice", "").strip()
        if not voice:
            return jsonify({"error": "缺少 voice（音色）"}), 400

        engine = payload.get("engine", "").strip() or tts_processor.DEFAULT_ENGINE
        language = payload.get("language", "cmn")
        rate = payload.get("rate", "+0%")
        volume = payload.get("volume", "+0%")
        pitch = payload.get("pitch", "+0Hz")

        processing_mode = payload.get("processing_mode", "full")
        if processing_mode not in ("mfa-only", "full"):
            processing_mode = "full"

        output_format = payload.get("format", "sv")
        project_title = payload.get("title", "Project")

        bpm = float(payload.get("bpm", 120))
        base_pitch = int(payload.get("base_pitch", 60))
        f0_method = payload.get("f0_method", "dio")
        f0_smooth = payload.get("f0_smooth", "true").lower() == "true"
        f0_device = payload.get("f0_device", "auto")
        crepe_model = payload.get("crepe_model", "full")
        f0_smooth_window = int(payload.get("f0_smooth_window", 5))
        use_double_precision = payload.get("precision", "single").lower() == "double"
        f0_floor = float(payload.get("f0_floor", 71.0))
        f0_ceil = float(payload.get("f0_ceil", 800.0))
        refine_pitch = payload.get("auto_note_pitch", "false").lower() == "true"
        export_pitch_line = payload.get("export_pitch_line", "true").lower() == "true"
        vsqx_pitch_smooth_window = int(payload.get("vsqx_pitch_smooth_window", 5))

        # TTS跟读固定使用 Qwen3-ForcedAligner，"对齐工具运行设备"控件在前端
        # 依然有效（仅生效对象从"用户选择的后端"变为固定的 qwen3_aligner）。
        aligner_device = payload.get("aligner_device", "").strip() or "auto"

        english_word_align = payload.get("english_word_align", "false").lower() == "true"
        word_phoneme_map = payload.get("word_phoneme_map", "false").lower() == "true"
        if word_phoneme_map and language != "jpn":
            english_word_align = True

        # 对齐辅助移调（半音）：TTS跟读固定使用 Qwen3-FA，高音色（尤其是
        # 叠加了正向"音调"滑块的女声/童声音色）逐句短音频偶发触发块级
        # 时间戳错序，可通过该参数在对齐前临时降调规避，不影响最终产物。
        align_pitch_shift_semitones = _parse_align_pitch_shift(
            payload.get("align_pitch_shift_semitones", 0)
        )

        dict_source = _normalize_dict_source(payload.get("dict_source", "default"))

        vsqx_singer = payload.get("vsqx_singer", "MIKU_V4_Chinese")
        vsqx_singer_id = payload.get("vsqx_singer_id", "BNGE7CP7EMTRSNC3")
        vsqx_singer_bs = int(payload.get("vsqx_singer_bs", 4))
        if output_format == "vsqx":
            vsqx_singer, vsqx_singer_id, vsqx_singer_bs = _select_vsqx_singer(language, "full")

        # 如果前端带上了 preview_id（用户点过"生成预览"），且这次提交的
        # 合成参数与预览时完全一致，直接复用预览阶段已经生成好的分句
        # 音频，后台任务只需要做对齐；否则（没点过预览 / 点完预览后又
        # 改了文本或参数）退回"先合成再对齐"的完整流程。
        preview_id = payload.get("preview_id", "").strip()
        preview_entry = _tts_preview_take(preview_id, {
            "text": text, "engine": engine, "voice": voice,
            "rate": rate, "volume": volume, "pitch": pitch, "language": language,
        }) if preview_id else None

        job_id = uuid.uuid4().hex
        set_job(job_id, status="queued", created_at=datetime.now().isoformat())

        logger.info(
            f"TTS跟读处理启动 (engine={engine}, mode={processing_mode}, voice={voice}, format={output_format}, "
            f"复用预览={'是' if preview_entry else '否'})，投递后台任务: {job_id}"
        )

        Thread(
            target=run_tts_pipeline_job,
            daemon=True,
            kwargs=dict(
                job_id=job_id, text=text, language=language, voice=voice, engine=engine,
                rate=rate, volume=volume, pitch=pitch,
                processing_mode=processing_mode,
                output_format=output_format, project_title=project_title,
                bpm=bpm, base_pitch=base_pitch,
                f0_method=f0_method, f0_smooth=f0_smooth,
                f0_smooth_window=f0_smooth_window,
                use_double_precision=use_double_precision,
                f0_floor=f0_floor, f0_ceil=f0_ceil,
                refine_pitch=refine_pitch, export_pitch_line=export_pitch_line,
                vsqx_pitch_smooth_window=vsqx_pitch_smooth_window,
                f0_device=f0_device, crepe_model=crepe_model,
                aligner_device=aligner_device,
                english_word_align=english_word_align,
                vsqx_singer=vsqx_singer, vsqx_singer_id=vsqx_singer_id,
                vsqx_singer_bs=vsqx_singer_bs,
                word_phoneme_map=word_phoneme_map, dict_source=dict_source,
                align_pitch_shift_semitones=align_pitch_shift_semitones,
                preview_segments_dir=(preview_entry or {}).get("segments_dir"),
                preview_sentences=(preview_entry or {}).get("sentences"),
                preview_wav_path=(preview_entry or {}).get("wav_path"),
            ),
        ).start()

        return jsonify({"success": True, "job_id": job_id, "status": "queued"}), 202

    except Exception as e:
        logger.exception("TTS 流程启动失败")
        return jsonify({"error": str(e)}), 500


@app.route("/api/mfa/download-model/<language>", methods=["POST"])
def download_mfa_model(language: str):
    """下载 MFA 模型"""
    try:
        valid_languages = ["cmn", "zh", "eng", "en", "jpn", "ja", "kor", "ko", "yue"]
        if language not in valid_languages:
            return jsonify({"error": f"不支持的语言: {language}"}), 400

        success, message = MFAChecker.download_model(language)

        if success:
            return jsonify({"success": True, "message": message}), 200
        return jsonify({"success": False, "error": message}), 400

    except Exception as e:
        logger.error("下载模型错误: %s", e, exc_info=True)
        return jsonify({"error": f"下载失败: {str(e)}"}), 500


@app.route("/api/work-dir/files", methods=["GET"])
def list_work_dir_files():
    """列出工作目录中的文件"""
    try:
        files = []
        
        # 列出 WAV 文件
        for wav_file in WORK_DIR.glob("*.wav"):
            files.append({
                "name": wav_file.name,
                "path": str(wav_file),
                "type": "audio",
                "size": wav_file.stat().st_size,
                "modified": wav_file.stat().st_mtime
            })
        
        # 列出 LAB 文件
        for lab_file in WORK_DIR.glob("*.lab"):
            files.append({
                "name": lab_file.name,
                "path": str(lab_file),
                "type": "label",
                "size": lab_file.stat().st_size,
                "modified": lab_file.stat().st_mtime
            })

        # 列出 MIDI 文件
        for mid_file in WORK_DIR.glob("*.mid"):
            files.append({
                "name": mid_file.name,
                "path": str(mid_file),
                "type": "midi",
                "size": mid_file.stat().st_size,
                "modified": mid_file.stat().st_mtime
            })
        
        # 列出工程文件
        for project_file in WORK_DIR.glob("**/*.ustx"):
            files.append({
                "name": project_file.name,
                "path": str(project_file),
                "type": "project",
                "size": project_file.stat().st_size,
                "modified": project_file.stat().st_mtime
            })

        for project_file in WORK_DIR.glob("**/*.svp"):
            files.append({
                "name": project_file.name,
                "path": str(project_file),
                "type": "project",
                "size": project_file.stat().st_size,
                "modified": project_file.stat().st_mtime
            })

        for project_file in WORK_DIR.glob("*.vsqx"):
            files.append({
                "name": project_file.name,
                "path": str(project_file),
                "type": "project",
                "size": project_file.stat().st_size,
                "modified": project_file.stat().st_mtime
            })
        
        # 按修改时间排序
        files.sort(key=lambda x: x['modified'], reverse=True)
        
        return jsonify({
            "success": True,
            "work_dir": str(WORK_DIR),
            "file_count": len(files),
            "files": files
        }), 200
        
    except Exception as e:
        logger.error("列出文件失败: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/work-dir/download/<path:filename>", methods=["GET"])
def download_work_file(filename: str):
    """下载工作目录中的文件"""
    try:
        # 获取文件的相对路径和名称
        filename = filename.replace('\\', '/')  # 规范化路径分隔符
        
        # 提取最后的文件名
        basename = filename.split('/')[-1]
        
        # 构造完整路径
        if 'projects' in filename:
            # 来自 projects 子目录
            file_path = WORK_DIR / 'projects' / basename
        else:
            # 来自工作目录
            file_path = WORK_DIR / basename
        
        logger.info(f"下载请求: {filename} -> {file_path}")
        
        # 安全检查
        try:
            file_path = file_path.resolve()
            work_dir_resolved = (WORK_DIR).resolve()
            
            if not str(file_path).startswith(str(work_dir_resolved)):
                return jsonify({"error": "无权访问该文件"}), 403
        except ValueError:
            return jsonify({"error": "路径错误"}), 400
        
        if not file_path.exists():
            logger.error(f"文件不存在: {file_path}")
            return jsonify({"error": f"文件不存在: {file_path}"}), 404
        
        if not file_path.is_file():
            return jsonify({"error": "不是文件"}), 400
        
        logger.info(f"下载文件成功: {file_path}")
        return send_from_directory(str(file_path.parent), file_path.name, as_attachment=True)
        
    except Exception as e:
        logger.error(f"下载文件失败: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/work-dir/clear", methods=["POST"])
def clear_work_dir():
    """清空工作目录"""
    try:
        import shutil
        
        # 只删除特定类型的文件
        patterns = ["*.wav", "*.lab", "*.mid", "**/*.ustx", "**/*.svp", "*.vsqx", "*.txt", "*.TextGrid"]
        
        deleted_count = 0
        for pattern in patterns:
            for file_path in WORK_DIR.glob(pattern):
                if file_path.is_file():
                    file_path.unlink()
                    deleted_count += 1
        
        logger.info(f"清空工作目录: 删除 {deleted_count} 个文件")
        
        return jsonify({
            "success": True,
            "message": f"已删除 {deleted_count} 个文件"
        }), 200
        
    except Exception as e:
        logger.error("清空工作目录失败: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# =====================================================================
# 字幕识别（视频/音频 → 逐句字幕，独立于 MFA 对齐管线）
# =====================================================================

SUBTITLE_DIR = (WORK_DIR / "subtitles").resolve()
SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)

# 字幕任务不复用 JOBS（避免与对齐任务的 job_id 空间混在一起、互相误清理），
# 单独一份内存字典，生命周期与主进程一致，足够覆盖"上传→识别→下载"这类
# 短会话场景。
_SUBTITLE_JOBS: Dict[str, dict] = {}
_SUBTITLE_JOBS_LOCK = Lock()


def _set_subtitle_job(job_id: str, **kwargs):
    with _SUBTITLE_JOBS_LOCK:
        _SUBTITLE_JOBS[job_id] = {**_SUBTITLE_JOBS.get(job_id, {}), **kwargs}


def _get_subtitle_job(job_id: str):
    with _SUBTITLE_JOBS_LOCK:
        return _SUBTITLE_JOBS.get(job_id)


@app.route("/api/subtitle/status", methods=["GET"])
def subtitle_status():
    """
    字幕功能依赖检查：ffmpeg 是否可用 + Qwen3-ASR 独立服务是否在线。
    前端进入字幕页面时先调用一次，据此展示"未就绪"提示，避免用户上传
    大文件后才发现依赖缺失。
    """
    ffmpeg_ok, ffmpeg_msg = subtitle_processor.check_ffmpeg_available()
    from alt_aligners import Qwen3ASRAligner
    qwen_ok, qwen_msg = Qwen3ASRAligner.check_available()
    return jsonify({
        "success": True,
        "ffmpeg": {"available": ffmpeg_ok, "message": ffmpeg_msg},
        "qwen3_asr": {"available": qwen_ok, "message": qwen_msg},
        "ready": ffmpeg_ok and qwen_ok,
    }), 200


@app.route("/api/subtitle/upload", methods=["POST"])
def subtitle_upload():
    """
    上传视频/音频文件，仅保存到字幕专用工作目录，不在这里做任何识别。
    返回的 media_id 供后续 /api/subtitle/recognize 引用；同时返回一个
    可直接用于 <video>/<audio> 标签播放的相对 URL。
    """
    try:
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"success": False, "error": "未提供文件"}), 400

        ext = Path(f.filename).suffix.lower()
        if ext not in (subtitle_processor.VIDEO_EXTS | subtitle_processor.AUDIO_EXTS):
            return jsonify({
                "success": False,
                "error": f"不支持的文件类型: {ext or '（无扩展名）'}",
            }), 400

        media_id = uuid.uuid4().hex
        media_dir = SUBTITLE_DIR / media_id
        media_dir.mkdir(parents=True, exist_ok=True)

        stem = sanitize_stem(f.filename)
        saved_name = f"{stem}{ext}"
        saved_path = media_dir / saved_name
        f.save(str(saved_path))

        is_video = subtitle_processor.is_video_file(str(saved_path))

        try:
            duration = subtitle_processor.probe_duration_sec(str(saved_path))
        except Exception as e:
            logger.warning(f"读取媒体时长失败（不影响后续识别）: {e}")
            duration = None

        return jsonify({
            "success": True,
            "media_id": media_id,
            "filename": f.filename,
            "is_video": is_video,
            "duration": duration,
            "play_url": f"/api/subtitle/media/{media_id}/{quote(saved_name)}",
        }), 200

    except Exception as e:
        logger.error(f"字幕媒体上传失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/subtitle/media/<media_id>/<path:filename>", methods=["GET"])
def subtitle_media_serve(media_id: str, filename: str):
    """
    提供已上传媒体文件的播放访问（<video>/<audio> 标签直接用这个 URL 作为
    src）。Flask 开发服务器不支持 HTTP Range 分块，大视频快进体验一般，
    但满足"上传后本地预览校对字幕"这个用途已经足够。
    """
    try:
        media_dir = (SUBTITLE_DIR / media_id).resolve()
        if not str(media_dir).startswith(str(SUBTITLE_DIR)):
            return jsonify({"error": "无权访问"}), 403
        file_path = (media_dir / filename).resolve()
        if not str(file_path).startswith(str(media_dir)) or not file_path.is_file():
            return jsonify({"error": "文件不存在"}), 404
        return send_from_directory(str(file_path.parent), file_path.name)
    except Exception as e:
        logger.error(f"字幕媒体访问失败: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/subtitle/recognize", methods=["POST"])
def subtitle_recognize():
    """
    异步启动字幕识别任务。body（JSON）：
      - media_id           : /api/subtitle/upload 返回的媒体 ID（必填）
      - language           : 语言代码，"auto" 或 zh/en/ja/... 等（默认 "auto"，
                              对应 Qwen3-ASR 自动语言检测）
      - device             : "auto"|"cpu"|"cuda"，转发给 qwen3_server.py（默认 "auto"）
      - max_chars          : 单条字幕最大字符数，超过则按标点二次拆分（默认 34）
      - allow_comma_split  : 是否把逗号/顿号也当作切分点（默认 False，
                              只在句末标点处切）。开启后遇到逗号就切成
                              下一条字幕，与长度无关
      - remove_punctuation : 是否在识别结果中移除标点符号（默认 False）
      - close_vad_gaps     : 是否开启"VAD 合并间隔"（默认 False）。开启后
                              相邻两条字幕间只要静音间隙大于
                              vad_gap_threshold_sec，就把这段间隙对半分
                              配到中点（不论间隙多长，都是均分而非收紧
                              到贴合），让两条字幕挨得更近，但不合并
                              文本、不减少条目数
      - vad_gap_threshold_sec : 触发这一处理的间隔下限（秒，默认 0.6）；
                              间隔小于等于该值时保持原样不动

    与 /api/pipeline/job/<job_id> 使用同一套"轮询进度"前端交互模式，但
    走独立的 job 存储（/api/subtitle/job/<job_id>），避免和对齐任务混在
    一起。
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        media_id = (data.get("media_id") or "").strip()
        if not media_id:
            return jsonify({"success": False, "error": "缺少 media_id"}), 400

        media_dir = (SUBTITLE_DIR / media_id).resolve()
        if not str(media_dir).startswith(str(SUBTITLE_DIR)) or not media_dir.is_dir():
            return jsonify({"success": False, "error": "媒体不存在或已过期，请重新上传"}), 404

        media_files = [p for p in media_dir.iterdir() if p.is_file()]
        if not media_files:
            return jsonify({"success": False, "error": "媒体目录为空，请重新上传"}), 404
        src_path = media_files[0]

        language = data.get("language", "auto")
        device = data.get("device", "auto")
        try:
            max_chars = int(data.get("max_chars", subtitle_processor.MAX_SUBTITLE_CHARS))
        except (TypeError, ValueError):
            max_chars = subtitle_processor.MAX_SUBTITLE_CHARS
        max_chars = max(8, min(max_chars, 120))
        allow_comma_split = bool(data.get("allow_comma_split", False))
        remove_punctuation = bool(data.get("remove_punctuation", False))
        close_vad_gaps = bool(data.get("close_vad_gaps", False))
        try:
            vad_gap_threshold_sec = float(data.get("vad_gap_threshold_sec", 0.6))
        except (TypeError, ValueError):
            vad_gap_threshold_sec = 0.6
        vad_gap_threshold_sec = max(0.05, min(vad_gap_threshold_sec, 5.0))

        ffmpeg_ok, ffmpeg_msg = subtitle_processor.check_ffmpeg_available()
        if not ffmpeg_ok:
            return jsonify({"success": False, "error": ffmpeg_msg}), 400

        job_id = uuid.uuid4().hex
        _set_subtitle_job(
            job_id,
            status="running",
            media_id=media_id,
            progress={"done": 0, "total": 0, "stage": "extract"},
        )

        def _run():
            try:
                work_wav = media_dir / "_extracted_16k.wav"
                if subtitle_processor.is_video_file(str(src_path)) or src_path.suffix.lower() != ".wav":
                    subtitle_processor.extract_audio(str(src_path), str(work_wav))
                else:
                    work_wav = src_path

                def _progress(done, total):
                    _set_subtitle_job(
                        job_id,
                        status="running",
                        progress={"done": done, "total": total, "stage": "recognize"},
                    )

                entries = subtitle_processor.transcribe_to_subtitles(
                    str(work_wav),
                    language=language,
                    device=device,
                    max_chars=max_chars,
                    progress_cb=_progress,
                    allow_comma_split=allow_comma_split,
                    remove_punctuation=remove_punctuation,
                    close_vad_gaps=close_vad_gaps,
                    vad_gap_threshold_sec=vad_gap_threshold_sec,
                )

                _set_subtitle_job(
                    job_id,
                    status="done",
                    result={
                        "success": True,
                        "entries": [e.to_dict() for e in entries],
                        "count": len(entries),
                    },
                )
            except Exception as e:
                logger.error(f"字幕识别任务失败: {e}", exc_info=True)
                _set_subtitle_job(job_id, status="failed", error=str(e))

        Thread(target=_run, daemon=True).start()

        return jsonify({"success": True, "job_id": job_id}), 200

    except Exception as e:
        logger.error(f"字幕识别启动失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/subtitle/job/<job_id>", methods=["GET"])
def subtitle_job_status(job_id: str):
    job = _get_subtitle_job(job_id)
    if job is None:
        return jsonify({"success": False, "error": "任务不存在或已过期"}), 404
    return jsonify({"success": True, "job": job}), 200


@app.route("/api/subtitle/export", methods=["POST"])
def subtitle_export():
    """
    将前端当前编辑好的字幕条目导出为 SRT/LRC/TXT 文本，直接在响应体里
    返回文本内容（字幕数据本就在前端内存中，不需要读写工作目录，前端
    收到文本后用 Blob 方式触发浏览器下载即可）。

    body（JSON）：
      - entries : [{"start": 1.2, "end": 3.4, "text": "..."}, ...]
      - format  : "srt" | "lrc" | "txt"
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        entries = data.get("entries") or []
        fmt = (data.get("format") or "srt").lower()
        if fmt not in subtitle_processor.EXPORTERS:
            return jsonify({"success": False, "error": f"不支持的导出格式: {fmt}"}), 400
        if not isinstance(entries, list) or not entries:
            return jsonify({"success": False, "error": "没有可导出的字幕内容"}), 400

        content = subtitle_processor.export_subtitles(entries, fmt)
        return jsonify({"success": True, "format": fmt, "content": content}), 200

    except Exception as e:
        logger.error(f"字幕导出失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/subtitle/split_entry", methods=["POST"])
def subtitle_split_entry():
    """
    把编辑区里的一条字幕手动拆成两条（"✂️ 拆分"按钮）。这条字幕此时可能
    已经被用户编辑、合并过，早就没有识别阶段的逐字时间戳了，所以是无状态
    的纯计算接口：前端把当前这一行的 start/end/text 传进来，后端按文本
    标点/长度比例算出一个拆分点，返回两条新的 {start, end, text}，不涉及
    磁盘或 job 状态（同 /api/subtitle/export 的"前端持有数据"模式）。

    body（JSON）：
      - start : 该条字幕当前开始时间（秒，必填）
      - end   : 该条字幕当前结束时间（秒，必填，需大于 start）
      - text  : 该条字幕当前文本（必填，允许为空字符串）
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        text = data.get("text", "")
        try:
            start = float(data.get("start"))
            end = float(data.get("end"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "start/end 必须是数字"}), 400

        if end <= start:
            return jsonify({"success": False, "error": "end 必须大于 start"}), 400
        if not isinstance(text, str):
            return jsonify({"success": False, "error": "text 必须是字符串"}), 400

        left, right = subtitle_processor.split_entry_manually(text, start, end)
        return jsonify({"success": True, "left": left, "right": right}), 200

    except Exception as e:
        logger.error(f"字幕手动拆分失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/subtitle/cleanup", methods=["POST"])
def subtitle_cleanup():
    """
    删除某个 media_id 对应的已上传媒体文件与中间产物，供前端在用户离开
    字幕页面 / 主动清除时调用，避免上传的大体积视频长期占用磁盘。
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        media_id = (data.get("media_id") or "").strip()
        if not media_id:
            return jsonify({"success": False, "error": "缺少 media_id"}), 400
        media_dir = (SUBTITLE_DIR / media_id).resolve()
        if not str(media_dir).startswith(str(SUBTITLE_DIR)):
            return jsonify({"success": False, "error": "无权访问"}), 403
        if media_dir.is_dir():
            shutil.rmtree(media_dir, ignore_errors=True)
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.error(f"字幕媒体清理失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


# =====================================================================
# 字幕导入（SRT/LRC → 按时间轴切分音频 → 固定用 Qwen3-FA 逐句强制对齐）
#   与 /api/subtitle/* （音频→字幕，ASR 方向）互为反向操作，独立成
#   subtitle_import.py 模块，不复用/不影响 subtitle_processor.py 的
#   识别流程。
# =====================================================================

@app.route("/api/subtitle-import/split", methods=["POST"])
def subtitle_import_split():
    """
    「对话文本框批量处理」的"导入字幕"入口：上传一份完整音频 + 一份
    SRT/LRC 字幕文件，后端按字幕时间轴把音频切成若干独立小 WAV（每条
    字幕一个），返回可供前端逐个下载拼装成 DialogueBox 的清单——前端
    随后把每个切片当作用户手动上传的 audio_{i}，文本作为 text_{i}，
    像"文件夹导入"一样批量追加为多个对话框，仍然走现有
    /api/dialogue/process（对齐后端固定选 Qwen3-ForcedAligner）。

    请求为 multipart/form-data：
      - audio_file    : 完整音频文件（必填）
      - subtitle_file : .srt 或 .lrc 字幕文件（必填；.txt 按内容自动
                        判断格式）

    响应：
      {
        "success": true,
        "session_id": str,        # 供 /api/subtitle-import/slice/<id>/<n> 下载切片
        "cues": [{"index": int, "text": str, "start": float, "end": float}, ...],
        "gap_count": int,         # 被跳过的静音间隙数量（不生成对话框）
        "gap_total_sec": float,
      }
    """
    try:
        audio_file = request.files.get("audio_file")
        subtitle_file = request.files.get("subtitle_file")
        if not audio_file or not audio_file.filename:
            return jsonify({"success": False, "error": "请上传完整音频文件"}), 400
        if not subtitle_file or not subtitle_file.filename:
            return jsonify({"success": False, "error": "请上传 SRT / LRC 字幕文件"}), 400

        ok, msg = subtitle_processor.check_ffmpeg_available()
        if not ok:
            return jsonify({"success": False, "error": msg}), 400

        session_id = uuid.uuid4().hex
        session_dir = WORK_DIR / f"_subtitle_import_{session_id}"
        session_dir.mkdir(parents=True, exist_ok=True)

        audio_path = session_dir / sanitize_stem(audio_file.filename or "audio")
        audio_path = audio_path.with_suffix(Path(audio_file.filename).suffix or ".wav")
        audio_file.save(str(audio_path))

        # 非 WAV 输入统一先转成 16k 单声道 WAV，切片 / 时长探测都基于这份
        # 转换后的 WAV，避免原始容器格式（mp3/m4a 等）在 ffmpeg -ss 精确
        # 切片时出现的时间戳误差。
        if audio_path.suffix.lower() != ".wav":
            wav_path = session_dir / "source.wav"
            subtitle_processor.extract_audio(str(audio_path), str(wav_path))
        else:
            wav_path = audio_path

        subtitle_bytes = subtitle_file.read()
        subtitle_text = _decode_subtitle_text(subtitle_bytes)
        audio_duration = subtitle_processor.probe_duration_sec(str(wav_path))

        fmt, cues = subtitle_import.parse_subtitle_file(
            subtitle_file.filename, subtitle_text, audio_duration_sec=audio_duration
        )
        if not cues:
            shutil.rmtree(str(session_dir), ignore_errors=True)
            return jsonify({"success": False, "error": f"未能从字幕文件（识别为 {fmt.upper()}）中解析出任何有效条目"}), 400

        split_result = subtitle_import.build_dialogue_boxes(
            str(wav_path), cues, work_dir=str(session_dir), stem_prefix="cue",
            audio_duration_sec=audio_duration,
        )
        if not split_result.get("success"):
            shutil.rmtree(str(session_dir), ignore_errors=True)
            return jsonify({"success": False, "error": split_result.get("error", "字幕切分失败")}), 400

        boxes = split_result["boxes"]
        gap_segments = split_result["gap_segments"]

        # 把切片路径登记到 job 存储里，供下载路由按 session_id + index 校验、
        # 定位到磁盘文件；不复用 JOBS（那是异步任务状态，这里是纯同步的
        # 一次性会话数据）。
        SUBTITLE_IMPORT_SESSIONS[session_id] = {
            "dir": str(session_dir),
            "slices": [b["wav_path"] for b in boxes],
        }

        cues_payload = [
            {"index": i, "text": b["text"], "start": b["start"], "end": b["end"]}
            for i, b in enumerate(boxes)
        ]
        gap_total = sum(g["end"] - g["start"] for g in gap_segments)

        return jsonify({
            "success": True,
            "session_id": session_id,
            "format": fmt,
            "cues": cues_payload,
            "gap_count": len(gap_segments),
            "gap_total_sec": gap_total,
        }), 200

    except Exception as e:
        logger.error(f"字幕导入切分失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/subtitle-import/slice/<session_id>/<int:index>", methods=["GET"])
def subtitle_import_slice(session_id: str, index: int):
    """下载 /api/subtitle-import/split 切出的第 index 个音频片段（WAV）。"""
    session = SUBTITLE_IMPORT_SESSIONS.get(session_id)
    if not session or index < 0 or index >= len(session["slices"]):
        abort(404)
    slice_path = Path(session["slices"][index])
    if not slice_path.exists():
        abort(404)
    directory = str(slice_path.parent)
    return send_from_directory(directory, slice_path.name, mimetype="audio/wav")


@app.route("/api/subtitle-import/cleanup", methods=["POST"])
def subtitle_import_cleanup():
    """前端完成对话框填充后调用，清理该会话的临时切片目录。"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        session_id = (data.get("session_id") or "").strip()
        session = SUBTITLE_IMPORT_SESSIONS.pop(session_id, None) if session_id else None
        if session:
            shutil.rmtree(session["dir"], ignore_errors=True)
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.error(f"字幕导入会话清理失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


def run_subtitle_align_job(job_id: str, wav_path: str, cues, language: str,
                            aligner_device, english_word_align: bool,
                            align_pitch_shift_semitones: float, audio_duration_sec: float,
                            processing_mode: str, original_text: str = "",
                            skip_split_every_n: int = 1, **project_kwargs):
    """
    单文件"字幕跟读"后台任务：整段音频按字幕时间轴逐句（或按
    skip_split_every_n 合并成块）Qwen3-FA 对齐，产出完整 LAB；"完整处理"
    模式下继续复用 process_project_only 做 F0 提取 + 工程文件生成（与
    其它单文件对齐流程一致，不再另写一套）。
    """
    try:
        set_job(job_id, status="running", started_at=datetime.now().isoformat(),
                progress={"done": 0, "total": len(cues)})

        def _progress_cb(done, total):
            set_job(job_id, status="running", progress={"done": done, "total": total})

        align_result = subtitle_import.align_subtitle_audio(
            wav_path, cues, language,
            aligner_device=aligner_device,
            english_word_align=english_word_align,
            align_pitch_shift_semitones=align_pitch_shift_semitones,
            audio_duration_sec=audio_duration_sec,
            progress_cb=_progress_cb,
            skip_split_every_n=skip_split_every_n,
        )

        if not align_result.get("success"):
            set_job(job_id, status="failed", finished_at=datetime.now().isoformat(),
                    error=align_result.get("error", "字幕跟读对齐失败"))
            return

        lab_content = align_result["lab_content"]

        if processing_mode != "full":
            # mfa-only 等价模式：只需要 LAB，不生成工程文件
            lab_path = str(Path(wav_path).with_suffix(".lab"))
            Path(lab_path).write_text(lab_content, encoding="utf-8")
            set_job(
                job_id, status="done", finished_at=datetime.now().isoformat(),
                result={
                    "success": True,
                    "lab_content": lab_content,
                    "lab_path": lab_path,
                    "audio_duration": align_result.get("audio_duration"),
                    "warnings": align_result.get("warnings", []),
                },
            )
            return

        lab_path = str(Path(wav_path).with_suffix(".lab"))
        Path(lab_path).write_text(lab_content, encoding="utf-8")

        # language 已经是本函数的顶层形参（对齐阶段就在用），不再从
        # project_kwargs 里取——project_kwargs 若同时携带同名 language，
        # 会在 Thread(kwargs=dict(language=..., **project_kwargs)) 处
        # 冲突（TypeError: dict() got multiple values for keyword
        # argument 'language'），这里统一只用外层这一份，显式转发。
        project_result = pipeline.process_project_only(
            wav_path=wav_path, lab_path=lab_path, midi_path=None,
            language=language, original_text=original_text,
            **project_kwargs,
        )

        # 【重要】process_project_only 的返回值本身不带 lab_content
        # （它是"生成工程文件"，LAB 只是内部中间产物）——其它输入模式
        # （音频跟读/TTS跟读）的"完整处理"结果因此也没有 LAB 标注内容
        # 可看，这是既有的统一行为，不在这里改动。但字幕跟读是一个全新
        # 功能，用户切分/对齐是否准确通常需要直接核对 LAB 才能确认，
        # 所以这里把已经生成好的 lab_content 一并塞进结果，让"完整处理"
        # 模式下前端也能展示"LAB 标注内容"标签页与下载/复制按钮
        # （见 result.labContent 相关的 v-if），不影响其它模式的行为。
        if project_result.get("success"):
            project_result.setdefault("lab_content", lab_content)
            project_result["warnings"] = align_result.get("warnings", [])
            set_job(job_id, status="done", finished_at=datetime.now().isoformat(), result=project_result)
        else:
            set_job(job_id, status="failed", finished_at=datetime.now().isoformat(),
                    error=project_result.get("error", "工程文件生成失败"), result=project_result)

    except Exception as e:
        logger.exception("字幕跟读后台任务异常")
        set_job(job_id, status="failed", finished_at=datetime.now().isoformat(), error=str(e))


@app.route("/api/subtitle-import/align", methods=["POST"])
def subtitle_import_align():
    """
    「单文件处理」的"字幕跟读"入口：上传一份完整音频 + 一份 SRT/LRC
    字幕文件，后端按字幕时间轴逐句固定用 Qwen3-ForcedAligner 强制对齐，
    产出覆盖整段音频的 LAB（异步任务，走与其它单文件流程一致的
    /api/pipeline/job/<job_id> 轮询）。

    请求为 multipart/form-data：
      - audio_file       : 完整音频文件（必填）
      - subtitle_file    : .srt 或 .lrc 字幕文件（必填）
      - language          : 语种代码（cmn/eng/jpn/kor/yue 等，默认 cmn）
      - aligner_device     : Qwen3-FA 运行设备（可选，默认 auto）
      - align_pitch_shift_semitones : 对齐辅助移调（半音，可选，默认 0）
      - english_word_align : 英语单词级对齐开关（可选，默认 false）
      - processing_mode    : "full"（默认，继续生成工程文件）或
                              "mfa-only"（仅产出 LAB，不生成工程文件）
      - 其余参数（title/format/bpm/f0_*/word_phoneme_map/dict_source/
        vsqx_singer 等）与 /api/pipeline/project-only 一致，仅在
        processing_mode="full" 时使用。

    "字幕每多少个时间轴跳过分割音频"（合并相邻字幕一起送 Qwen3-FA 对齐，
    减少切分次数）不作为本请求的表单参数，而是读取设置页面保存的全局
    设置项 subtitle_import_skip_split_every_n（见 app_settings.py），
    与其它 Qwen3-FA 调优参数保持一致的"设置页统一管理"方式。
    """
    try:
        audio_file = request.files.get("audio_file")
        subtitle_file = request.files.get("subtitle_file")
        if not audio_file or not audio_file.filename:
            return jsonify({"error": "请上传完整音频文件"}), 400
        if not subtitle_file or not subtitle_file.filename:
            return jsonify({"error": "请上传 SRT / LRC 字幕文件"}), 400

        ok, msg = subtitle_processor.check_ffmpeg_available()
        if not ok:
            return jsonify({"error": msg}), 400

        language = request.form.get("language", "cmn")
        aligner_device = request.form.get("aligner_device", "").strip() or None
        english_word_align = request.form.get("english_word_align", "false").lower() == "true"
        # word_phoneme_map（"英语单词→音素映射"，把混在文本中的英语单词
        # 写入 SVP/VSQX 的 phonemes 字段）依赖对齐阶段已经把这些英语单词
        # 作为独立单元切分对齐好——这一步正是 english_word_align 在做的事
        # （关闭时，混合文本里的英语单词会被 Qwen3-FA 当成普通文字/拼音
        # 处理，产出类似 "vocaloty"/"tts" 这种把整个英语单词错误吞成一个
        # "音节" 的 LAB，再被当作拼音音素写进工程文件，导致发音错乱）。
        # 因此这里必须和其它路由（TTS跟读 /api/tts/process、音频跟读
        # /api/pipeline/full、/mfa-only、对话批量 /api/dialogue/process）
        # 保持完全一致的联动：只要用户打开了 word_phoneme_map 且语言不是
        # 日语，就自动一并打开 english_word_align，不要求用户重复勾选两个
        # 开关。此前字幕跟读遗漏了这一步，是本次要修复的问题。
        word_phoneme_map_probe = request.form.get("word_phoneme_map", "false").lower() == "true"
        if word_phoneme_map_probe and language != "jpn":
            english_word_align = True
        align_pitch_shift_semitones = _parse_align_pitch_shift(
            request.form.get("align_pitch_shift_semitones", 0)
        )
        processing_mode = request.form.get("processing_mode", "full")
        if processing_mode not in ("full", "mfa-only"):
            processing_mode = "full"

        # 【重要】build_job_paths 返回的 wav_path 固定以 .wav 结尾（用于最终
        # 产物命名），但这里的原始上传可能是 mp3/m4a 等——不能直接把非 WAV
        # 字节流存进一个 .wav 后缀的文件（后续 ffmpeg -ss 精确切片 / 后端其它
        # 环节都会按 WAV 容器解析，容器与内容不符会解析失败或产出静音）。
        # 因此原始文件按自身真实扩展名单独落盘，非 WAV 一律先用 ffmpeg 转成
        # 16k 单声道 WAV（转换后的路径才是后续切片/时长探测使用的 wav_path），
        # 与 subtitle_processor.py 里"先统一转码"的约定一致。
        stem, wav_path_obj, _ = build_job_paths(audio_file.filename or "subtitle_audio.wav")
        original_ext = Path(audio_file.filename or "").suffix or ".wav"
        original_path = WORK_DIR / f"{stem}_orig{original_ext}"
        audio_file.save(str(original_path))

        if original_ext.lower() == ".wav":
            wav_path = str(wav_path_obj)
            shutil.copy(str(original_path), wav_path)
        else:
            wav_path = str(wav_path_obj)
            subtitle_processor.extract_audio(str(original_path), wav_path)

        subtitle_bytes = subtitle_file.read()
        subtitle_text = _decode_subtitle_text(subtitle_bytes)
        audio_duration = subtitle_processor.probe_duration_sec(wav_path)

        fmt, cues = subtitle_import.parse_subtitle_file(
            subtitle_file.filename, subtitle_text, audio_duration_sec=audio_duration
        )
        if not cues:
            return jsonify({"error": f"未能从字幕文件（识别为 {fmt.upper()}）中解析出任何有效条目"}), 400

        project_kwargs = {}
        if processing_mode == "full":
            output_format = request.form.get("format", "sv")
            project_kwargs = dict(
                project_title=request.form.get("title", "Subtitle Project"),
                output_format=output_format,
                bpm=float(request.form.get("bpm", 120)),
                base_pitch=int(request.form.get("base_pitch", 60)),
                f0_method=request.form.get("f0_method", "dio"),
                f0_device=request.form.get("f0_device", "auto"),
                crepe_model=request.form.get("crepe_model", "full"),
                f0_smooth=request.form.get("f0_smooth", "true").lower() == "true",
                f0_smooth_window=int(request.form.get("f0_smooth_window", 5)),
                use_double_precision=request.form.get("precision", "single").lower() == "double",
                f0_floor=float(request.form.get("f0_floor", 71.0)),
                f0_ceil=float(request.form.get("f0_ceil", 800.0)),
                refine_pitch=request.form.get("auto_note_pitch", "false").lower() == "true",
                export_pitch_line=request.form.get("export_pitch_line", "true").lower() == "true",
                vsqx_pitch_smooth_window=int(request.form.get("vsqx_pitch_smooth_window", 5)),
                word_phoneme_map=request.form.get("word_phoneme_map", "false").lower() == "true",
                dict_source=_normalize_dict_source(request.form.get("dict_source", "default")),
            )
            if output_format == "vsqx":
                vsqx_singer, vsqx_singer_id, vsqx_singer_bs = _select_vsqx_singer(language, "full")
                project_kwargs["vsqx_singer"] = request.form.get("vsqx_singer", vsqx_singer)
                project_kwargs["vsqx_singer_id"] = request.form.get("vsqx_singer_id", vsqx_singer_id)
                project_kwargs["vsqx_singer_bs"] = int(request.form.get("vsqx_singer_bs", vsqx_singer_bs))

        job_id = uuid.uuid4().hex
        set_job(job_id, status="queued", created_at=datetime.now().isoformat())

        logger.info(
            f"字幕跟读启动 (format={fmt}, cues={len(cues)}, mode={processing_mode})，"
            f"投递后台任务: {job_id}"
        )

        # 【重要】拼接全部字幕原文（按时间顺序），供 process_project_only 内部
        # 预提取 native_english_words 使用——与"音频跟读"（process_full 把用户
        # 输入的整段 text 转发为 original_text）、"TTS跟读"（同样传 text）保持
        # 一致的判定依据。若不传，_label_is_english_word 会在非英语语言下回退
        # 到词典查询（is_in_english_dict），一旦某条字幕里的英语单词不在词典中，
        # 就会被当成普通拼音/文字处理，被 Qwen3-FA 整体吞成一个"音节"写进 LAB
        # （如 "vocaloty"/"tts"），而不是先按英语单词切分再做 G2P/音素映射——
        # 这正是此前字幕跟读遗漏的一步。
        subtitle_original_text = "\n".join(cue.text for cue in cues if cue.text)

        # "字幕每多少个时间轴跳过分割音频"：全局设置项，仅影响字幕跟读
        # 这一个功能，实时读取（保存设置后下一次任务立即生效）。
        skip_split_every_n = app_settings.get_subtitle_import_skip_split_every_n()

        Thread(
            target=run_subtitle_align_job,
            daemon=True,
            kwargs=dict(
                job_id=job_id, wav_path=wav_path, cues=cues, language=language,
                aligner_device=aligner_device, english_word_align=english_word_align,
                align_pitch_shift_semitones=align_pitch_shift_semitones,
                audio_duration_sec=audio_duration, processing_mode=processing_mode,
                original_text=subtitle_original_text,
                skip_split_every_n=skip_split_every_n,
                **project_kwargs,
            ),
        ).start()

        return jsonify({"success": True, "job_id": job_id, "status": "queued", "cue_count": len(cues)}), 202

    except Exception as e:
        logger.error(f"字幕跟读启动失败: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def open_browser(host: str, port: int):
    sleep(2)
    webbrowser.open(f"http://{host}:{port}")


def main(host: str = "127.0.0.1", port: int = 5000):
    # 命令提示符窗口显示/隐藏：读取设置页面保存的配置，决定 app.py 自己
    # 这个终端窗口是否隐藏。放在最前面调用，若设置为隐藏则连下面的启动
    # 横幅也不会被看到——这是预期行为（用户主动选择了"启动即隐藏"）。
    # 之后用户在设置页面切换该开关时，update_settings() 里还会对本进程
    # 再调用一次，做到无需重启主服务即可立即生效。
    try:
        app_settings.apply_console_visibility()
    except Exception as e:
        logger.warning(f"⚠️  设置控制台窗口显示状态失败（不影响服务本身运行）: {e}")

    print(f"\n{'=' * 60}")
    print("🚀 启动 SVS Lab Tools with MFA + PyWORLD")
    print(f"📍 访问地址: http://{host}:{port}")
    print(f"📂 工作目录: {WORK_DIR}")
    print(f"📂 前端目录: {FRONTEND_DIST}")
    print(f"⏹️  按 Ctrl+C 停止服务")
    print(f"{'=' * 60}\n")
    
    # 由 launcher.py 拉起时会注入 SVS_SKIP_AUTO_BROWSER=1：界面已经交给
    # launcher 里的 pywebview 原生窗口负责显示，这里就不再自己弹一个系统
    # 浏览器标签页了，否则每次启动会同时看到"原生窗口 + 浏览器标签页"。
    # 单独用 `python app.py` 调试（不经过 launcher）时不受影响，仍会自动开浏览器。
    if os.environ.get("SVS_SKIP_AUTO_BROWSER") != "1":
        Thread(target=open_browser, args=(host, port), daemon=True).start()
    else:
        logger.info("检测到由 launcher 启动（SVS_SKIP_AUTO_BROWSER=1），跳过自动打开系统浏览器。")
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()