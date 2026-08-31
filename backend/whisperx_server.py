# whisperx_server.py
#
# WhisperX 独立服务
# https://github.com/m-bain/whisperx
#
# 【2026-08 架构调整】此前 WhisperX 是本项目里唯一一个直接在主 Flask 进程
# （.mfa_env）内 `import whisperx` 并本地加载模型的对齐后端——alt_aligners.py
# 里的 WhisperXAligner 曾经是这样，Qwen3ASRAligner / NeMoForcedAligner 那时
# 反而已经是纯 HTTP 客户端了。这个不一致导致 requirements.txt 必须同时满足
# whisperx==3.2.0（精确锁定 transformers==4.39.3 / faster-whisper==1.0.0）
# 与 qwen-asr（精确锁定 transformers==4.57.6）两套互斥的版本要求。
#
# 现在反过来：qwen-asr 迁回主进程 .mfa_env（详见 qwen3_server.py 已下线、
# alt_aligners.py 里 Qwen3ASRAligner / Qwen3ForcedAligner 改为直接
# `from qwen_asr import ...` 本地加载的说明），WhisperX 换成与 NeMo 完全
# 同样的独立服务模式：单独装一个 conda/venv 环境（.whisperx_env），跑成
# 本地 HTTP 微服务，主进程（alt_aligners.py 里的 WhisperXAligner）只通过
# HTTP 调用它，不在 .mfa_env 里 import whisperx。
#
# 用法：
#   conda create --prefix ./.whisperx_env python=3.10 -y
#   conda activate ./.whisperx_env
#   pip install -r requirements-whisperx.txt
#   python whisperx_server.py
#
# 默认监听 127.0.0.1:5854（5851 已被历史遗留脚本占用参考、5852 已被
# nemo_server.py 占用、5853 已被 qwen3tts_server.py 占用）。
from __future__ import annotations

from flask import Flask, request, jsonify
from pathlib import Path
import os
import sys
import time
import logging
import threading
import subprocess
import warnings
from typing import Any, Dict, List, Optional, Tuple

# 【2026-08 变更，修复"直接用 python 启动本脚本时，命令行只显示启动阶段
# 那几行日志，之后处理请求产生的日志只在日志文件里能看到，命令行窗口不再
# 更新"】原因和修法与 app.py / nemo_server.py / qwen3_server.py 头部同一处
# 改动完全一致：Python 判断 sys.stdout 是否连着真正的交互式终端来决定用
# 行缓冲还是全缓冲，这个判断在 Windows 上不总是可靠，退化成全缓冲后，日志
# 会攒在内部缓冲区里迟迟不刷新到命令行窗口（但 logging 模块如果配了
# FileHandler，每次 emit() 都会立刻 flush，不受影响，所以日志文件里反而
# 更全）。用 reconfigure(line_buffering=True) 强制改成行缓冲，不再依赖
# 自动判断。--noconsole/由 launcher.py 以 CREATE_NO_WINDOW 拉起时
# sys.stdout/sys.stderr 可能是 None，判空后跳过，不影响正常发布环境下的
# 运行。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(line_buffering=True)
        except Exception:
            pass
del _stream

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 项目目录现在直接是 backend 目录
BACKEND_DIR = Path(__file__).resolve().parent

# WhisperX（faster-whisper）模型缓存——独立复用一份缓存目录，不与
# qwen3-asr 迁入主进程后使用的 HF 缓存共享，避免多进程/同进程不同缓存
# 目录写入产生混淆。
_WHISPER_CACHE = BACKEND_DIR / "models" / "whisperx_cache"
_WHISPER_CACHE.mkdir(parents=True, exist_ok=True)

# 必须在导入 huggingface_hub / whisperx 相关包之前设置
os.environ.setdefault("HF_HOME", str(_WHISPER_CACHE))

# 引入 app_settings 并应用前端保存的配置 (替代写死的 HF_HUB_OFFLINE)
try:
    from app_settings import apply_env_from_settings as _apply_hf_env_settings
    _apply_hf_env_settings()
except Exception as _settings_err:
    logger.warning(f"⚠️  读取模型下载设置失败（{_settings_err}），回退到默认自动检测模型更新模式")
    os.environ["HF_HUB_OFFLINE"] = "0"

# 命令提示符窗口显示/隐藏：同样读取设置页面保存的配置。与上面的
# HF_HUB_OFFLINE 不同，这个开关每次调用都能立即生效（不是"仅启动时读取
# 一次"），放在这里调用一次即可覆盖"进程刚启动时窗口该不该隐藏"；
# 之后 /restart 触发的自重启会拉起全新进程，新进程执行到这里时会重新读
# 到最新设置，无需额外处理。仅在 Windows 上生效，其余平台直接跳过。
# 【2026-08 起】launcher.py 正常启动本进程时用 CREATE_NO_WINDOW，本身
# 就没有控制台窗口，这里调用是无操作，详见 qwen3_server.py 里的同名说明。
try:
    from app_settings import apply_console_visibility as _apply_console_visibility
    _apply_console_visibility()
except Exception as _console_err:
    logger.warning(f"⚠️  设置控制台窗口显示状态失败（不影响服务本身运行）: {_console_err}")

logger.info(f"HF_HOME = {os.environ.get('HF_HOME')}")
logger.info(f"HF_HUB_OFFLINE = {os.environ.get('HF_HUB_OFFLINE')}")
logger.info(f"HF_ENDPOINT = {os.environ.get('HF_ENDPOINT', 'Official (Not Set)')}")

# 当前支持的 Whisper 模型列表，与 alt_aligners.py 里
# WhisperXAligner.SUPPORTED_MODELS 保持一致（由前端选择器引用）。
SUPPORTED_MODELS: List[str] = [
    "large-v3",
    "large-v3-turbo",
    "large-v2",
    "medium",
    "small",
    "base",
    "tiny",
]

# 惰性加载并缓存的模型槽位。ASR 模型按 (whisper_model, device, compute_type)
# 缓存；对齐模型按 lang_code 缓存（同一语言的 wav2vec2 对齐模型可在不同
# ASR 模型/请求间复用）。
_asr_models: Dict[Tuple[str, str, str], Any] = {}
_align_models: Dict[str, Tuple[Any, Any]] = {}   # lang_code -> (model_a, metadata)
_model_lock = threading.Lock()

# 当前 HTTP server 实例（在 __main__ 里用 werkzeug.serving.make_server 创建），
# /restart 需要拿到它才能在重启前"干净地"关闭监听端口，见 restart() 里的说明。
_httpd = None


def _safe_device(requested: str) -> str:
    """
    与 alt_aligners.py 里同名函数用途一致（独立进程，无法直接 import，
    自行维护一份）：CUDA smoke-test，避免 CPU-only torch 或驱动异常时
    torch.cuda.is_available() 误报 True 导致后续调用直接崩溃。
    """
    import torch

    req = (requested or "auto").lower()
    if req == "cpu":
        return "cpu"
    if req not in ("cuda", "auto"):
        req = "auto"

    if not torch.cuda.is_available():
        if req == "cuda":
            logger.warning("⚠️  请求 CUDA 但未检测到可用 GPU，回退到 CPU")
        return "cpu"

    try:
        torch.zeros(1, device="cuda")
        return "cuda"
    except Exception as e:
        logger.warning(f"⚠️  CUDA 初始化失败（{e}），回退到 CPU")
        return "cpu"


def _resolve_compute_type(compute_type: str, device: str) -> str:
    """与 alt_aligners.py 里 WhisperXAligner._resolve_compute_type 逻辑一致。"""
    if device == "cpu":
        return "int8" if compute_type in ("float16", "int8_float16") else compute_type

    if compute_type not in ("float16", "int8_float16"):
        return compute_type

    try:
        import ctranslate2
        supported = ctranslate2.get_supported_compute_types("cuda")
        if compute_type not in supported:
            fallback = "int8" if "int8" in supported else "float32"
            logger.warning(
                f"[WhisperX] 当前 GPU 不支持 {compute_type} "
                f"(支持: {supported})，自动切换为 {fallback}"
            )
            return fallback
        return compute_type
    except Exception:
        logger.warning(
            f"[WhisperX] 无法查询 GPU compute_type 支持情况，"
            f"保守切换: {compute_type} → int8"
        )
        return "int8"


def _is_cuda_oom_or_env_error(exc: Exception) -> bool:
    """与 nemo_server.py / qwen3_server.py 里同名函数用途一致。"""
    msg = str(exc).lower()
    _KEYWORDS = (
        "out of memory",
        "cuda out of memory",
        "cublas_status_alloc_failed",
        "cudnn_status_not_initialized",
        "cuda error",
        "no cuda gpus are available",
        "cuda driver",
        "cuda toolkit",
        "cuda-capable device",
        "cuda initialization",
        "cuda_error",
        "device-side assert",
        "nvrtc",
        "nvml",
    )
    return any(kw in msg for kw in _KEYWORDS)


def load_asr_model(whisper_model: str, device_override: str = "auto", compute_type: str = "float16"):
    """
    惰性加载并缓存 WhisperX ASR 模型，按优先级构建 compute_type 尝试链
    （float16 → int8 → float32），与 alt_aligners.py 原 WhisperXAligner._load_asr()
    行为一致。
    """
    device = _safe_device(device_override)
    resolved_ct = _resolve_compute_type(compute_type, device)

    cache_key = (whisper_model, device, resolved_ct)
    with _model_lock:
        if cache_key in _asr_models:
            return _asr_models[cache_key], device, resolved_ct

        import whisperx

        _FALLBACK: Dict[str, list] = {
            "float16":      ["int8", "float32"],
            "int8_float16": ["int8", "float32"],
            "int8":         ["float32"],
        }
        candidates = [resolved_ct] + _FALLBACK.get(resolved_ct, [])

        last_exc: Optional[Exception] = None
        for ct in candidates:
            try:
                logger.info(f"[WhisperX] 加载 ASR 模型: {whisper_model} (device={device}, compute_type={ct})")
                model = whisperx.load_model(
                    whisper_model, device, compute_type=ct, download_root=str(_WHISPER_CACHE),
                )
                final_key = (whisper_model, device, ct)
                _asr_models[final_key] = model
                logger.info(f"[WhisperX] ✓ ASR 模型已加载 (compute_type={ct})")
                return model, device, ct
            except ValueError as e:
                err_lower = str(e).lower()
                if "compute type" in err_lower or "float16" in err_lower:
                    logger.warning(f"[WhisperX] compute_type={ct} 失败: {e}，尝试下一档...")
                    last_exc = e
                else:
                    raise

        raise last_exc or RuntimeError("[WhisperX] 所有 compute_type 均失败，请检查 GPU 驱动")


def load_align_model(lang_code: str, device: str):
    with _model_lock:
        if lang_code in _align_models:
            return _align_models[lang_code]

        import whisperx
        logger.info(f"[WhisperX] 加载对齐模型: {lang_code}")
        model_a, metadata = whisperx.load_align_model(language_code=lang_code, device=device)
        _align_models[lang_code] = (model_a, metadata)
        logger.info(f"[WhisperX] ✓ 对齐模型 ({lang_code}) 已加载")
        return model_a, metadata


def _load_audio(audio_path: str):
    """
    与 alt_aligners.py 原实现一致：whisperx.load_audio() 依赖 ffmpeg
    子进程；若环境中 ffmpeg 不可用则回退到 soundfile + librosa 重采样。
    """
    import whisperx

    _SR = 16_000
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*torchcodec.*")
            return whisperx.load_audio(audio_path)
    except Exception as _ffmpeg_err:
        logger.warning(f"[WhisperX] whisperx.load_audio 失败（{_ffmpeg_err}），尝试用 soundfile + librosa 回退加载…")
        import soundfile as _sf
        import numpy as _np
        _data, _orig_sr = _sf.read(audio_path, always_2d=False)
        if _data.ndim > 1:
            _data = _data.mean(axis=1)
        _data = _data.astype(_np.float32)
        if _orig_sr != _SR:
            import librosa as _librosa
            _data = _librosa.resample(_data, orig_sr=_orig_sr, target_sr=_SR)
        logger.info(f"[WhisperX] soundfile 回退加载成功: {len(_data)/float(_SR):.2f}s @ {_SR}Hz")
        return _data


def _transcribe_with_oom_retry(model, audio, wx_lang: str, whisper_model: str,
                                device_override: str, compute_type: str, batch_size: int) -> Dict:
    """
    对 model.transcribe() 的一层 batch_size 自适应重试封装：遇到 CUDA
    显存不足时自动腰斩 batch_size 重试，直到 batch_size=1 仍然失败才
    真正把异常抛给调用方。与 alt_aligners.py 原
    WhisperXAligner._transcribe_with_oom_retry() 行为一致。
    """
    last_exc: Optional[Exception] = None
    bs = max(1, int(batch_size))
    while bs >= 1:
        try:
            return model.transcribe(audio, batch_size=bs, language=wx_lang)
        except RuntimeError as e:
            if "out of memory" not in str(e).lower():
                raise
            last_exc = e
            logger.warning(f"[WhisperX] ASR 转录 CUDA 显存不足（batch_size={bs}），尝试释放显存缓存并腰斩 batch_size 重试…")
            try:
                import torch as _torch_oom
                if _torch_oom.cuda.is_available():
                    _torch_oom.cuda.empty_cache()
            except Exception:
                pass
            if bs == 1:
                break
            bs = max(1, bs // 2)

    raise RuntimeError(
        f"CUDA 显存不足，即使把 batch_size 降到 1 仍然失败——当前 GPU 剩余显存可能已经不够运行 "
        f"{whisper_model} 模型本身。建议在设置里把 whisperx_batch_size 调得更小，或把使用的 "
        f"Whisper 模型档位换成更小的（medium / small / base），也可以检查一下是否有其他进程占用了显存。"
        f"原始错误: {last_exc}"
    )


@app.get("/")
def health():
    return jsonify(
        {
            "success": True,
            "message": "WhisperX service is running",
            "asr_models_loaded": [f"{m}@{d}({ct})" for (m, d, ct) in _asr_models.keys()],
            "align_models_loaded": list(_align_models.keys()),
            "supported_models": SUPPORTED_MODELS,
        }
    )


@app.post("/restart")
def restart():
    """
    优雅自重启，让设置页面保存的模型下载配置立刻生效。已加载到显存/内存
    里的模型会随进程重建一起释放，重启后按需重新惰性加载，属于预期行为。

    做法与 nemo_server.py / qwen3_server.py 完全一致（"先干净关闭、再拉起
    全新进程"，不使用 os.execv，理由见二者 restart() 顶部说明）：
      1) 显式调用 _httpd.shutdown() + server_close()，确保端口被完全释放；
      2) 端口释放后用 subprocess.Popen 启动全新 python 进程；
      3) 显式传入 stdout=sys.stdout, stderr=sys.stderr，让重启前后日志
         连续不丢失（launcher.py 用 CREATE_NO_WINDOW 拉起时，标准句柄已
         被重定向到 logs/whisperx.log 文件）；
      4) 最后 os._exit(0) 立即结束旧进程。
    """
    def _delayed_restart():
        time.sleep(0.5)
        logger.info("⟳ 收到重启请求，正在重启 whisperx_server.py 进程以应用最新设置...")

        global _httpd
        try:
            if _httpd is not None:
                _httpd.shutdown()
                _httpd.server_close()
                logger.info("✓ 已释放端口 5854，准备拉起新进程")
        except Exception as e:
            logger.warning(f"关闭旧 HTTP server 时出现异常（继续重启流程）: {e}")

        python = sys.executable
        try:
            subprocess.Popen(
                [python] + sys.argv,
                close_fds=True,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
        except Exception as e:
            logger.error(f"启动新进程失败: {e}", exc_info=True)

        os._exit(0)

    threading.Thread(target=_delayed_restart, daemon=True).start()
    return jsonify({"success": True, "message": "WhisperX 服务正在重启..."})


@app.post("/transcribe")
def transcribe():
    """
    仅做一次 ASR 转录（不做后续 wav2vec2 强制对齐），供 alt_aligners.py
    里 WhisperXAligner._transcribe_rough_segments() 复用——Qwen3-FA 长
    音频分段规划用它的时间戳+识别字数配额来规划分段边界（详见
    alt_aligners.py 里 _plan_chunks_via_whisperx_rough_pass() 顶部说明），
    不使用它识别出的文字内容本身。

    请求体:
      {"audio": "本机绝对路径", "language": "en"/"zh"/... (whisperx 语言码),
       "whisper_model": "large-v3", "device": "auto"|"cpu"|"cuda",
       "compute_type": "float16", "batch_size": 16}

    返回:
      {"success": true, "raw_segments": [...]}  （whisperx transcribe() 原始
      segments，每项至少含 start/end/text）
    """
    data = request.get_json(force=True) or {}
    audio_path = data.get("audio")
    if not audio_path:
        return jsonify({"success": False, "error": "缺少 audio 参数"}), 400
    audio_path = str(audio_path)
    if not Path(audio_path).exists():
        return jsonify({"success": False, "error": "音频文件不存在"}), 400

    wx_lang = data.get("language") or "en"
    whisper_model = data.get("whisper_model", "large-v3")
    device_override = data.get("device", "auto")
    compute_type = data.get("compute_type", "float16")
    batch_size = data.get("batch_size", 16)

    try:
        audio = _load_audio(audio_path)
        model, device, ct = load_asr_model(whisper_model, device_override, compute_type)
        asr_out = _transcribe_with_oom_retry(model, audio, wx_lang, whisper_model, device_override, ct, batch_size)
        raw_segments = asr_out.get("segments", [])
        if not raw_segments:
            return jsonify({"success": False, "error": "WhisperX ASR 无输出，请检查音频质量"})
        return jsonify({"success": True, "raw_segments": raw_segments})
    except ImportError as e:
        # 【2026-09 修复】曾在 pkg_resources 缺失（ctranslate2 导入期间
        # `import pkg_resources` 找不到 setuptools）时排查困难：whisperx
        # 是惰性导入的，只有首次真正的 /transcribe 请求才会触发，此前
        # 这里只返回 JSON 错误、不打日志，命令行窗口看起来"什么都没
        # 发生"。改用 logger.error(..., exc_info=True) 确保无论日志
        # handler/缓冲情况如何，完整 traceback 都会被记录下来。
        logger.error(f"[WhisperX] /transcribe 导入依赖失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": f"whisperx 未安装或依赖缺失: {e}"}), 500
    except Exception as e:
        logger.error(f"[WhisperX][粗测] ASR 转录失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.post("/align")
def align():
    """
    ASR 转录 + 逐句 wav2vec2 强制对齐，一次请求内完成，返回逐句的对齐
    结果（局部时间轴 + 句子偏移，已经是全局绝对时间）。

    这里只做"调用模型拿原始对齐结果"这一件事：音频加载、ASR 转录、逐句
    裁剪、wav2vec2 对齐、char/word 条目提取。分句边界规划之外的一切后
    处理（标点停顿注入、CTC 拉伸修复、能量精修静音边界、音素时长守护、
    LAB 文本组装等）全部保留在客户端（alt_aligners.py 的 WhisperXAligner
    类里），不搬过来——那些逻辑不依赖 whisperx 本身，没有理由拉长每次
    HTTP 往返，也避免这个服务进程和客户端进程的后处理逻辑出现两份、日后
    改一处忘改另一处。

    请求体:
      {
        "audio": "本机绝对路径",
        "raw_segments": [{"start": .., "end": .., "text": ".."}, ...]
          客户端已完成 ASR 转录 + 参考文本绑定的句段列表（每句已绑定好
          要对齐的文本，见 alt_aligners.py align() 步骤 1-3）。
        "language": "en"/"zh"/...（whisperx 语言码，用于选择对齐模型）,
        "device": "auto"|"cpu"|"cuda"
      }

    返回:
      {
        "success": true,
        "segments": [
          {"idx": 0, "start_sec": .., "end_sec": .., "entries": [[s,e,text], ...]},
          ...
        ]
      }
      某一句对齐异常时，该句 "entries" 退化为整句一条目（与原 in-process
      实现的降级行为一致），不中断其余句子。
    """
    data = request.get_json(force=True) or {}
    audio_path = data.get("audio")
    raw_segments = data.get("raw_segments")
    wx_lang = data.get("language") or "en"
    device_override = data.get("device", "auto")

    if not audio_path:
        return jsonify({"success": False, "error": "缺少 audio 参数"}), 400
    if not raw_segments:
        return jsonify({"success": False, "error": "缺少 raw_segments 参数"}), 400
    audio_path = str(audio_path)
    if not Path(audio_path).exists():
        return jsonify({"success": False, "error": "音频文件不存在"}), 400

    try:
        import whisperx

        device = _safe_device(device_override)
        audio = _load_audio(audio_path)
        model_a, metadata = load_align_model(wx_lang, device)

        _SR = 16_000
        out_segments: List[Dict[str, Any]] = []

        for idx, seg in enumerate(raw_segments):
            start_sec = float(seg.get("start", 0.0))
            end_sec = float(seg.get("end", 0.0))
            seg_text = (seg.get("text") or "").strip()

            if not seg_text or end_sec <= start_sec:
                continue

            st_samp = max(0, int(start_sec * _SR))
            en_samp = min(len(audio), int(end_sec * _SR))
            cropped = audio[st_samp:en_samp]

            if len(cropped) < 160:
                logger.warning(f"[WhisperX] 第 {idx+1} 句裁剪后过短（{len(cropped)} samples），跳过")
                continue

            local_seg_list = [{"text": seg_text, "start": 0.0, "end": end_sec - start_sec}]
            entries: List[List[Any]] = []
            try:
                local_aligned = whisperx.align(
                    local_seg_list, model_a, metadata, cropped, device,
                    return_char_alignments=True,
                )
                for a_seg in local_aligned.get("segments", []):
                    chars = a_seg.get("chars", [])
                    words = a_seg.get("words", [])
                    # 客户端会按语言决定用 chars 还是 words，这里两者都
                    # 一并返回，避免服务端也要重复一份"哪些语言用字符级"
                    # 的判断逻辑（该逻辑已经在 alt_aligners.py 里维护）。
                    for unit in chars:
                        s = unit.get("start"); e = unit.get("end")
                        t = (unit.get("char") or unit.get("text") or "").strip()
                        if s is not None and e is not None:
                            entries.append([float(s) + start_sec, float(e) + start_sec, t, "char"])
                    for unit in words:
                        s = unit.get("start"); e = unit.get("end")
                        t = (unit.get("word") or unit.get("text") or "").strip()
                        if s is not None and e is not None:
                            entries.append([float(s) + start_sec, float(e) + start_sec, t, "word"])
            except Exception as exc:
                logger.error(f"[WhisperX] 第 {idx+1} 句对齐异常（'{seg_text[:30]}'）: {exc}")
                entries = [[start_sec, end_sec, seg_text, "word"]]

            out_segments.append({
                "idx": idx,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "entries": entries,
            })

        return jsonify({"success": True, "segments": out_segments})

    except ImportError as e:
        return jsonify({"success": False, "error": f"whisperx 未安装: {e}，请执行 pip install whisperx"}), 500
    except Exception as e:
        logger.error(f"[WhisperX] 对齐失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    # 生产环境建议改成 waitress / gevent / gunicorn，与 qwen3tts_server.py /
    # nemo_server.py 一致
    #
    # 这里不用 app.run(...)，改用 werkzeug.serving.make_server(...) 拿到
    # 底层 server 对象存进 _httpd —— /restart 需要它来在重启前调用
    # shutdown() + server_close() 干净地释放端口，见 restart() 里的说明。
    from werkzeug.serving import make_server

    _httpd = make_server("127.0.0.1", 5854, app)
    logger.info("🚀 WhisperX service listening on http://127.0.0.1:5854")
    _httpd.serve_forever()
