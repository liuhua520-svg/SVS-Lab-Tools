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
import sys
import uuid
import json
import logging
import webbrowser
from urllib.parse import quote
from threading import Thread
from time import sleep
from pathlib import Path
from typing import Dict

from flask import Flask, request, jsonify, send_from_directory, abort, Response
from flask_cors import CORS
import requests

from mfa_utils import MFAChecker
from mfa_processor import MFAProcessor
from pipeline import AudioProcessingPipeline
from alt_aligners import get_alt_aligner_status
import dictionary_manager
import app_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


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
            whisperx_model=whisperx_model,
            nemo_model=(nemo_model or None),
            english_word_align=english_word_align,
            vsqx_singer=vsqx_singer,
            vsqx_singer_id=vsqx_singer_id,
            vsqx_singer_bs=vsqx_singer_bs,
            word_phoneme_map=word_phoneme_map,
            dict_source=dict_source,
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

        # NeMo Forced Aligner 模型覆盖（可选）：留空则由 NeMoForcedAligner
        # 按语言使用内置默认模型（见 alt_aligners.NeMoForcedAligner.LANGUAGE_MODELS）
        nemo_model = request.form.get("nemo_model", "").strip()

        english_word_align = request.form.get("english_word_align", "false").lower() == "true"
        word_phoneme_map   = request.form.get("word_phoneme_map",   "false").lower() == "true"

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
            ),
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
                     english_word_align: bool = False):
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
                                           whisperx_model=whisperx_model,
                                           nemo_model=(nemo_model or None),
                                           english_word_align=english_word_align)

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

        whisperx_model = request.form.get("whisperx_model", "large-v3")
        if whisperx_model not in ("large-v3", "large-v3-turbo", "large-v2", "medium", "small", "base", "tiny"):
            whisperx_model = "large-v3"

        # NeMo Forced Aligner 模型覆盖（可选）：留空则由 NeMoForcedAligner
        # 按语言使用内置默认模型
        nemo_model = request.form.get("nemo_model", "").strip()

        english_word_align = request.form.get("english_word_align", "false").lower() == "true"

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
                  whisperx_model, nemo_model, english_word_align),
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

def run_dialogue_batch_job(job_id: str, boxes, **kwargs):
    try:
        set_job(
            job_id,
            status="running",
            started_at=datetime.now().isoformat(),
            progress={"done": 0, "total": len(boxes)},
        )

        def _progress_cb(done, total, box_result):
            set_job(
                job_id,
                status="running",
                progress={"done": done, "total": total},
                last_box=box_result,
            )

        result = pipeline.process_dialogue_batch(boxes, progress_cb=_progress_cb, **kwargs)

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
      - box_count       : 对话框总数
      - text_{i}        : 第 i 个对话框的台词文本（可选）
      - audio_{i}       : 第 i 个对话框的音频文件（可选，缺失则该对话框跳过；每框限 1 个音频）
      - lab_{i}         : 第 i 个对话框的 LAB 标注文件（可选；提供时跳过对齐，
                          直接使用该 LAB 生成对应音轨——最高优先级）
      - mid_{i}         : 第 i 个对话框的 MIDI 文件（可选；无 lab_{i} 时提供则跳过对齐，
                          从 MIDI 音符自动生成段落 + 读取 BPM/音高；.mid/.midi）
      - notation_{i}    : 统一入口，等价于 lab_{i} / mid_{i} 之一（按扩展名自动识别，
                          与 /api/pipeline/project-only 的 notation_file 语义一致）；
                          若同时提供 lab_{i}/mid_{i}，以它们为准。
      - processing_mode : "full"（默认，完整处理：无 LAB/MIDI 的框走对齐）或
                          "project-only"（仅生成工程：跳过对齐，无 LAB/MIDI 的框直接跳过）
      - phoneme_mode    : "none"（默认）/"merge"/"hiragana"/"katakana"，仅对来自
                          LAB 的段落、且输出格式非 USTX 时生效
      - format          : "sv" / "vsqx" / "ustx"（USTX 原生支持多音轨）
      - 其余参数与 /api/pipeline/full 基本一致（language / title / bpm / f0_* /
        aligner_backend / word_phoneme_map / dict_source 等），对全部对话框统一生效。
    """
    try:
        box_count = int(request.form.get("box_count", 0))
        if box_count <= 0:
            return jsonify({"error": "box_count 必须大于 0"}), 400
        if box_count > 64:
            return jsonify({"error": "对话框数量过多（上限 64）"}), 400

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

        whisperx_model = request.form.get("whisperx_model", "large-v3")
        if whisperx_model not in ("large-v3", "large-v3-turbo", "large-v2", "medium", "small", "base", "tiny"):
            whisperx_model = "large-v3"

        nemo_model = request.form.get("nemo_model", "").strip()
        english_word_align = request.form.get("english_word_align", "false").lower() == "true"
        word_phoneme_map   = request.form.get("word_phoneme_map",   "false").lower() == "true"

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

            audio_path = None
            lab_path = None
            midi_path = None

            if audio_file is not None and audio_file.filename:
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

            boxes.append({
                "index": i,
                "text": text,
                "audio_path": audio_path,
                "lab_path": lab_path,
                "midi_path": midi_path,
            })

        if not any(b["audio_path"] for b in boxes):
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
            f"对话文本框批量处理启动 (mode={processing_mode}, backend={aligner_backend}, "
            f"format={output_format}, boxes={box_count})，投递后台任务: {job_id}"
        )

        Thread(
            target=run_dialogue_batch_job,
            daemon=True,
            kwargs=dict(
                job_id=job_id,
                boxes=boxes,
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
                whisperx_model=whisperx_model,
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