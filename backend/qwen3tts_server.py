# qwen3tts_server.py
#
# Qwen3-TTS 独立服务（"TTS跟读"下"选择 TTS"引擎新增的 Qwen3-TTS 方案）
# https://github.com/QwenLM/Qwen3-TTS
# https://huggingface.co/spaces/Qwen/Qwen3-TTS
#
# 与 qwen3_server.py（Qwen3-ASR / Qwen3-ForcedAligner，端口 5001）、
# nemo_server.py（NeMo Forced Aligner，端口 5002）同样的理由：qwen-tts 这
# 个官方包对 transformers / torch 的版本要求，与 qwen_asr（qwen3_server.py
# 所在环境）不完全一致（qwen-tts 依赖更新的 transformers 版本以支持其
# 12Hz tokenizer 架构），装进同一个 venv 容易互相"打架"、把对方需要的
# 版本降级。因此照搬同样的做法：Qwen3-TTS 单独装一个 conda/venv 环境，
# 跑成一个本地 HTTP 微服务，主进程（tts_processor.py 里的
# Qwen3TTSClient）只通过 HTTP 调用它，不在主 .mfa_env 里 import qwen_tts。
#
# 用法：
#   conda create --prefix ./.qwen3tts_env python=3.12 -y
#   conda activate ./.qwen3tts_env
#   pip install -r requirements-qwen3tts.txt
#   python qwen3tts_server.py
#
# 默认监听 127.0.0.1:5003（5001 已被 qwen3_server.py 占用，
# 5002 已被 nemo_server.py 占用）。
#
# 支持 Qwen3-TTS 官方三种模式（与 GitHub README「Python Package Usage」
# 完全对应，见 https://github.com/QwenLM/Qwen3-TTS#python-package-usage）：
#   - CustomVoice ：预设音色 + 可选自然语言风格指令（generate_custom_voice）
#   - VoiceDesign ：仅凭文本描述"设计"一个新音色（generate_voice_design）
#   - VoiceClone  ：Base 模型，导入参考音频（+ 可选参考文本）克隆音色
#                   （generate_voice_clone；x_vector_only_mode=True 时不
#                   需要参考文本，但克隆质量可能下降）
from __future__ import annotations

from flask import Flask, request, jsonify
from pathlib import Path
import base64
import os
import sys
import time
import logging
import threading
import subprocess
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 项目目录现在直接是 backend 目录
BACKEND_DIR = Path(__file__).resolve().parent

# 缓存固定到当前应用内，与 qwen3_server.py / nemo_server.py 各自独立，
# 避免三个进程同时写同一个 hub 缓存目录产生竞态。
CACHE_DIR = BACKEND_DIR / "models" / "qwen3tts_hf_cache"
HUB_CACHE_DIR = CACHE_DIR / "hub"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
HUB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 必须在导入 qwen_tts / transformers 相关包之前设置
os.environ["HF_HOME"] = str(CACHE_DIR)
os.environ["HF_HUB_CACHE"] = str(HUB_CACHE_DIR)

# 引入 app_settings 并应用前端保存的配置 (替代写死的 HF_HUB_OFFLINE)
try:
    from app_settings import apply_env_from_settings as _apply_hf_env_settings
    _apply_hf_env_settings()
except Exception as _settings_err:
    logger.warning(f"⚠️  读取模型下载设置失败（{_settings_err}），回退到默认自动检测模型更新模式")
    os.environ["HF_HUB_OFFLINE"] = "0"

# 命令提示符窗口显示/隐藏：同样读取设置页面保存的配置，仅在 Windows 上
# 生效，其余平台直接跳过（详见 qwen3_server.py 里的同名说明）。
try:
    from app_settings import apply_console_visibility as _apply_console_visibility
    _apply_console_visibility()
except Exception as _console_err:
    logger.warning(f"⚠️  设置控制台窗口显示状态失败（不影响服务本身运行）: {_console_err}")

logger.info(f"HF_HOME = {os.environ.get('HF_HOME')}")
logger.info(f"HF_HUB_CACHE = {os.environ.get('HF_HUB_CACHE')}")
logger.info(f"HF_HUB_OFFLINE = {os.environ.get('HF_HUB_OFFLINE')}")
logger.info(f"HF_ENDPOINT = {os.environ.get('HF_ENDPOINT', 'Official (Not Set)')}")

# ═════════════════════════════════════════════════════════════════════════
# 模型注册表：mode（custom_voice / voice_design / voice_clone） × size（1.7B / 0.6B）
#
# VoiceDesign 目前官方只发布了 1.7B 权重（见 README「Released Models」表
# 只有一行 VoiceDesign），选择 0.6B 规模时该模式自动回退到 1.7B，并在
# /status、/generate 的返回体里如实标注实际使用的规模，避免"设置里选了
# 0.6B，日志却在下载 1.7B 模型"看起来像 bug。
# ═════════════════════════════════════════════════════════════════════════

MODEL_IDS: Dict[str, Dict[str, str]] = {
    "custom_voice": {
        "1.7B": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "0.6B": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
    },
    "voice_design": {
        "1.7B": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        # 官方暂未发布 0.6B-VoiceDesign，统一回退到 1.7B。
        "0.6B": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    },
    "voice_clone": {
        "1.7B": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "0.6B": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
    },
}

# CustomVoice 模型内置的 9 个预设音色（原生语种见 README 表格），供前端
# "选择音色"下拉框展示，不需要请求一次模型才能拿到列表。
CUSTOM_VOICE_SPEAKERS: List[Dict[str, str]] = [
    {"id": "Vivian",    "name": "Vivian",    "gender": "female", "locale": "zh", "desc": "明亮、略带锋芒感的年轻女声"},
    {"id": "Serena",    "name": "Serena",    "gender": "female", "locale": "zh", "desc": "温暖、柔和的年轻女声"},
    {"id": "Uncle_Fu",  "name": "Uncle_Fu",  "gender": "male",   "locale": "zh", "desc": "低沉醇厚的成熟男声"},
    {"id": "Dylan",     "name": "Dylan",     "gender": "male",   "locale": "zh", "desc": "清亮自然的北京腔年轻男声（京片子）"},
    {"id": "Eric",      "name": "Eric",      "gender": "male",   "locale": "zh", "desc": "略带沙哑明亮感的成都腔男声（四川话）"},
    {"id": "Ryan",      "name": "Ryan",      "gender": "male",   "locale": "en", "desc": "节奏感强、充满活力的男声"},
    {"id": "Aiden",     "name": "Aiden",     "gender": "male",   "locale": "en", "desc": "阳光、中音清亮的美式男声"},
    {"id": "Ono_Anna",  "name": "Ono_Anna",  "gender": "female", "locale": "ja", "desc": "俏皮轻盈的日语女声"},
    {"id": "Sohee",     "name": "Sohee",     "gender": "female", "locale": "ko", "desc": "温暖且富有感情的韩语女声"},
]

# 内部语言短代码（与前端"语言"下拉一致：cmn/eng/jpn/kor/yue 等）→
# generate_* 系列函数需要的 language 参数（官方示例用英文全称，也支持
# "Auto"/省略做自动语种识别）。
_LANG_SHORT_TO_QWEN: Dict[str, str] = {
    "zh": "Chinese", "cmn": "Chinese",
    "yue": "Chinese",  # Qwen3-TTS 暂无独立粤语标签，按中文大类处理，由模型自适应方言/口音
    "en": "English", "eng": "English",
    "ja": "Japanese", "jpn": "Japanese",
    "ko": "Korean", "kor": "Korean",
    "de": "German", "fr": "French", "ru": "Russian",
    "pt": "Portuguese", "es": "Spanish", "it": "Italian",
}


def _qwen_language(language: Optional[str]) -> str:
    if not language:
        return "Auto"
    key = str(language).strip()
    return _LANG_SHORT_TO_QWEN.get(key.lower(), key or "Auto")


# ═════════════════════════════════════════════════════════════════════════
# 设备 / dtype 选择（与 qwen3_server.py 完全一致的策略，独立维护一份，
# 三个进程互相独立，无法直接 import 对方模块）
# ═════════════════════════════════════════════════════════════════════════

def _pick_device_and_dtype(device_override: str = "auto"):
    import torch

    if device_override == "cpu":
        return "cpu", torch.float32

    if not torch.cuda.is_available():
        if device_override == "cuda":
            logger.warning("⚠️  请求 CUDA 但未检测到可用 GPU，回退到 CPU")
        return "cpu", torch.float32

    try:
        torch.zeros(1, device="cuda")
    except Exception as e:
        logger.warning(f"⚠️  CUDA 初始化失败（{e}），回退到 CPU")
        return "cpu", torch.float32

    device = "cuda:0"
    try:
        props = torch.cuda.get_device_properties(0)
        cc_major = props.major
        logger.info(f"GPU: {props.name}  compute capability: {cc_major}.{props.minor}")

        if cc_major >= 8:
            dtype = torch.bfloat16
        elif cc_major >= 6:
            dtype = torch.float16
        else:
            dtype = torch.float32

        logger.info(f"自动选择 dtype: {dtype}")
        return device, dtype
    except Exception as e:
        logger.warning(f"⚠️  GPU 能力查询失败（{e}），使用 float32 保底")
        return device, torch.float32


def _is_cuda_oom_or_env_error(exc: Exception) -> bool:
    """判断异常是否属于"显存不足"或"CUDA 环境本身有问题"，与
    qwen3_server.py / nemo_server.py / alt_aligners.py 里同名函数用途一致
    （各进程相互独立，无法直接 import，各自维护一份）。"""
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


# ═════════════════════════════════════════════════════════════════════════
# 模型缓存：按 (mode, size, device) 分别惰性加载并缓存，三个模式互不共享
# 权重（VoiceDesign / CustomVoice / Base 是三套完全独立的 checkpoint），
# 但允许同时驻留多个模型（例如"Voice Design then Clone"工作流需要
# VoiceDesign + Base 两个模型同时可用）。显存吃紧时可通过 /unload 手动
# 释放不再需要的模式。
# ═════════════════════════════════════════════════════════════════════════

_models: Dict[Tuple[str, str, str], Any] = {}
_models_lock = threading.Lock()

# 当前 HTTP server 实例（用法与 qwen3_server.py 一致，供 /restart 使用）
_httpd = None


def _model_key(mode: str, size: str, device_override: str) -> Tuple[str, str, str]:
    return (mode, size, device_override)


def load_model(mode: str, size: str = "1.7B", device_override: str = "auto"):
    """
    惰性加载并缓存指定模式/规模/设备的 Qwen3-TTS 模型。

    显存不足自动降级：与 qwen3_server.py load_model() 同样的两级降级
    策略——GPU 加载失败且命中 OOM/CUDA 环境异常时，自动整体切换到 CPU
    重新加载（Qwen3-TTS 没有 qwen_asr 那样的 max_inference_batch_size
    概念，这里没有"腰斩 batch_size"这一级，直接降到 CPU）。
    """
    if mode not in MODEL_IDS:
        raise ValueError(f"未知 Qwen3-TTS 模式: {mode}")
    if size not in ("1.7B", "0.6B"):
        size = "1.7B"

    model_id = MODEL_IDS[mode][size]
    key = _model_key(mode, size, device_override)

    with _models_lock:
        cached = _models.get(key)
        if cached is not None:
            return cached

        import torch
        from qwen_tts import Qwen3TTSModel

        device_map, dtype = _pick_device_and_dtype(device_override)
        logger.info(f"正在加载 Qwen3-TTS[{mode}/{size}]：{model_id}（device={device_map}, dtype={dtype}）")

        def _build(_device_map: str, _dtype):
            kwargs = dict(device_map=_device_map, dtype=_dtype)
            # flash_attention_2 只在支持的 GPU + CUDA 环境下才尝试启用，
            # 未安装 flash-attn 或环境不支持时静默回退到默认 attention
            # 实现，不应该让整个模型加载失败。
            if _device_map.startswith("cuda"):
                try:
                    import flash_attn  # noqa: F401
                    kwargs["attn_implementation"] = "flash_attention_2"
                except ImportError:
                    pass
            return Qwen3TTSModel.from_pretrained(model_id, **kwargs)

        try:
            model = _build(device_map, dtype)
            _models[key] = model
            logger.info(f"✅ Qwen3-TTS[{mode}/{size}] 加载成功！服务已就绪。")
            return model
        except Exception as e:
            if device_map.startswith("cuda") and _is_cuda_oom_or_env_error(e):
                logger.warning(f"⚠️  在 GPU 上加载 Qwen3-TTS[{mode}/{size}] 失败（{e}），自动切换到 CPU 重新加载...")
                try:
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                try:
                    model = _build("cpu", torch.float32)
                    cpu_key = _model_key(mode, size, "cpu")
                    _models[cpu_key] = model
                    logger.info(f"✅ Qwen3-TTS[{mode}/{size}] 加载成功（已回退到 CPU）！服务已就绪。")
                    return model
                except Exception as e2:
                    logger.error(f"❌ CPU 兜底加载仍然失败: {e2}", exc_info=True)
                    raise
            logger.error(f"❌ Qwen3-TTS[{mode}/{size}] 加载失败: {e}", exc_info=True)
            raise


def unload_model(mode: Optional[str] = None, size: Optional[str] = None) -> int:
    """释放已加载的模型，返回释放数量。mode/size 均为 None 时释放全部。"""
    with _models_lock:
        keys_to_remove = [
            k for k in _models
            if (mode is None or k[0] == mode) and (size is None or k[1] == size)
        ]
        for k in keys_to_remove:
            del _models[k]
    if keys_to_remove:
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    return len(keys_to_remove)


# ═════════════════════════════════════════════════════════════════════════
# 参考音频输入统一处理：前端可能传本地路径（与主进程共享同一台机器的
# work_dir 场景）或 base64 编码的音频字节（跨进程/跨机器场景更通用）。
# ═════════════════════════════════════════════════════════════════════════

def _resolve_ref_audio(payload: Dict) -> Any:
    """
    返回 qwen_tts 的 generate_voice_clone / create_voice_clone_prompt 可
    直接接受的 ref_audio 参数值：本地文件路径（字符串）或临时落盘后的路径。

    payload 支持二选一：
      - "ref_audio_path": 本地绝对路径（app.py 与本服务同机时最简单，
        参考音频已经在上传阶段落盘到工作目录）；
      - "ref_audio_base64": base64 编码的音频字节 + 可选 "ref_audio_ext"
        （默认 ".wav"），本服务落盘为临时文件后传给 qwen_tts。
    """
    ref_audio_path = (payload.get("ref_audio_path") or "").strip()
    if ref_audio_path:
        if not Path(ref_audio_path).exists():
            raise ValueError(f"参考音频文件不存在: {ref_audio_path}")
        return ref_audio_path

    ref_audio_b64 = payload.get("ref_audio_base64")
    if ref_audio_b64:
        ext = (payload.get("ref_audio_ext") or ".wav").strip()
        if not ext.startswith("."):
            ext = "." + ext
        tmp_path = Path(tempfile.gettempdir()) / f"qwen3tts_ref_{uuid.uuid4().hex[:10]}{ext}"
        tmp_path.write_bytes(base64.b64decode(ref_audio_b64))
        return str(tmp_path)

    raise ValueError("缺少参考音频：需要提供 ref_audio_path 或 ref_audio_base64")


def _save_wav_and_encode(wav, sr: int) -> Tuple[str, str]:
    """把生成结果落盘为临时 wav 并返回 (临时路径, base64 编码字符串)。"""
    import soundfile as sf

    tmp_path = Path(tempfile.gettempdir()) / f"qwen3tts_out_{uuid.uuid4().hex[:10]}.wav"
    sf.write(str(tmp_path), wav, sr, subtype="PCM_16")
    audio_b64 = base64.b64encode(tmp_path.read_bytes()).decode("ascii")
    return str(tmp_path), audio_b64


# ═════════════════════════════════════════════════════════════════════════
# HTTP 路由
# ═════════════════════════════════════════════════════════════════════════

@app.get("/")
def health():
    with _models_lock:
        loaded = [{"mode": k[0], "size": k[1], "device": k[2]} for k in _models.keys()]
    return jsonify(
        {
            "success": True,
            "message": "Qwen3-TTS service is running",
            "loaded_models": loaded,
            "available_modes": list(MODEL_IDS.keys()),
        }
    )


@app.get("/speakers")
def speakers():
    """CustomVoice 模式的 9 个预设音色列表，供前端"选择音色"下拉框使用。"""
    return jsonify({"success": True, "speakers": CUSTOM_VOICE_SPEAKERS})


@app.post("/restart")
def restart():
    """
    优雅自重启，与 qwen3_server.py 的 /restart 实现完全一致（"先干净关闭、
    再拉起全新独立进程"两步法，避免 execv 在 Windows 上的重复重启坑，见
    qwen3_server.py 里的详细说明）。
    """
    def _delayed_restart():
        time.sleep(0.5)
        logger.info("⟳ 收到重启请求，正在重启 qwen3tts_server.py 进程以应用最新设置...")

        global _httpd
        try:
            if _httpd is not None:
                _httpd.shutdown()
                _httpd.server_close()
                logger.info("✓ 已释放端口 5003，准备拉起新进程")
        except Exception as e:
            logger.warning(f"关闭旧 HTTP server 时出现异常（继续重启流程）: {e}")

        python = sys.executable
        try:
            subprocess.Popen([python] + sys.argv, close_fds=True)
        except Exception as e:
            logger.error(f"启动新进程失败: {e}", exc_info=True)

        os._exit(0)

    threading.Thread(target=_delayed_restart, daemon=True).start()
    return jsonify({"success": True, "message": "Qwen3-TTS 服务正在重启..."})


@app.post("/unload")
def unload():
    """释放已加载模型（显存紧张时可手动调用）。body: {mode?, size?}"""
    data = request.get_json(force=True, silent=True) or {}
    n = unload_model(data.get("mode") or None, data.get("size") or None)
    return jsonify({"success": True, "unloaded": n})


@app.post("/generate/custom_voice")
def generate_custom_voice():
    """
    CustomVoice：预设音色 + 可选风格指令。
    body: {
      "text": str, "speaker": str（CUSTOM_VOICE_SPEAKERS 里的 id）,
      "language"?: str（内部短代码或 Qwen 语种全称，缺省 "Auto"）,
      "instruct"?: str（自然语言风格指令，如"用特别愤怒的语气说"）,
      "size"?: "1.7B"|"0.6B"（缺省 "1.7B"）, "device"?: "auto"|"cpu"|"cuda"
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        text = (data.get("text") or "").strip()
        speaker = (data.get("speaker") or "").strip()
        if not text:
            return jsonify({"success": False, "error": "缺少 text 参数"}), 400
        if not speaker:
            return jsonify({"success": False, "error": "缺少 speaker（预设音色）参数"}), 400

        size = data.get("size") or "1.7B"
        device = data.get("device") or "auto"
        language = _qwen_language(data.get("language"))
        instruct = (data.get("instruct") or "").strip() or None

        model = load_model("custom_voice", size, device)
        wavs, sr = model.generate_custom_voice(
            text=text, language=language, speaker=speaker, instruct=instruct,
        )
        _, audio_b64 = _save_wav_and_encode(wavs[0], sr)
        return jsonify({"success": True, "audio_base64": audio_b64, "sample_rate": sr})
    except Exception as e:
        logger.error(f"[Qwen3-TTS/CustomVoice] 生成失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.post("/generate/voice_design")
def generate_voice_design():
    """
    VoiceDesign：仅凭自然语言描述"设计"一个新音色。
    body: {
      "text": str, "instruct": str（音色描述，如"体现撒娇稚嫩的萝莉女声..."）,
      "language"?: str, "size"?: "1.7B"|"0.6B"（0.6B 自动回退到 1.7B）,
      "device"?: "auto"|"cpu"|"cuda"
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        text = (data.get("text") or "").strip()
        instruct = (data.get("instruct") or "").strip()
        if not text:
            return jsonify({"success": False, "error": "缺少 text 参数"}), 400
        if not instruct:
            return jsonify({"success": False, "error": "缺少 instruct（声音描述）参数"}), 400

        size = data.get("size") or "1.7B"
        device = data.get("device") or "auto"
        language = _qwen_language(data.get("language"))

        model = load_model("voice_design", size, device)
        wavs, sr = model.generate_voice_design(
            text=text, language=language, instruct=instruct,
        )
        _, audio_b64 = _save_wav_and_encode(wavs[0], sr)
        return jsonify({"success": True, "audio_base64": audio_b64, "sample_rate": sr})
    except Exception as e:
        logger.error(f"[Qwen3-TTS/VoiceDesign] 生成失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.post("/generate/voice_clone")
def generate_voice_clone():
    """
    VoiceClone（Base 模型）：导入参考音频（+ 可选参考文本）克隆音色。
    body: {
      "text": str,
      "ref_audio_path"? / "ref_audio_base64"?（二选一，见 _resolve_ref_audio）,
      "ref_audio_ext"?: str（配合 ref_audio_base64 使用，默认 ".wav"）,
      "ref_text"?: str（x_vector_only=True 时可省略）,
      "x_vector_only"?: bool（默认 False，True 时不需要 ref_text 但克隆质量可能下降）,
      "language"?: str, "size"?: "1.7B"|"0.6B", "device"?: "auto"|"cpu"|"cuda"
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    tmp_ref_path: Optional[str] = None
    try:
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"success": False, "error": "缺少 text 参数"}), 400

        x_vector_only = bool(data.get("x_vector_only", False))
        ref_text = (data.get("ref_text") or "").strip()
        if not x_vector_only and not ref_text:
            return jsonify({
                "success": False,
                "error": "未开启「仅 x-vector」模式时必须提供 ref_text（参考音频的转录内容）",
            }), 400

        ref_audio = _resolve_ref_audio(data)
        # 只有 base64 落盘的情况才需要事后清理临时文件；本地路径场景下
        # 该文件属于主进程管理的工作目录，不应该被这里删除。
        if data.get("ref_audio_base64") and not data.get("ref_audio_path"):
            tmp_ref_path = ref_audio

        size = data.get("size") or "1.7B"
        device = data.get("device") or "auto"
        language = _qwen_language(data.get("language"))

        model = load_model("voice_clone", size, device)
        wavs, sr = model.generate_voice_clone(
            text=text,
            language=language,
            ref_audio=ref_audio,
            ref_text=(ref_text or None),
            x_vector_only_mode=x_vector_only,
        )
        _, audio_b64 = _save_wav_and_encode(wavs[0], sr)
        return jsonify({"success": True, "audio_base64": audio_b64, "sample_rate": sr})
    except Exception as e:
        logger.error(f"[Qwen3-TTS/VoiceClone] 生成失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if tmp_ref_path:
            try:
                Path(tmp_ref_path).unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == "__main__":
    # 生产环境建议改成 waitress / gevent / gunicorn
    #
    # 这里不用 app.run(...)，改用 werkzeug.serving.make_server(...) 拿到
    # 底层 server 对象存进 _httpd —— /restart 需要它来在重启前调用
    # shutdown() + server_close() 干净地释放端口，见 restart() 里的说明。
    from werkzeug.serving import make_server

    _httpd = make_server("127.0.0.1", 5003, app)
    logger.info("🚀 Qwen3-TTS service listening on http://127.0.0.1:5003")
    _httpd.serve_forever()
