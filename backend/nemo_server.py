# nemo_server.py
#
# NeMo Forced Aligner (NFA) 独立服务
# https://github.com/NVIDIA-NeMo/Speech/tree/main/tools/nemo_forced_aligner
#
# 与 qwen3_server.py 同样的理由：nemo_toolkit 对 packaging / fsspec /
# omegaconf / hydra-core / lightning 等核心依赖有严格的版本限制，装进主
# Flask 进程所在的 .mfa_env 会跟其它包（比如 pipdeptree 要求的
# packaging>=26）发生版本冲突，把这些包"降级"。所以照搬 Qwen3-ASR 的
# 做法：NeMo 单独装一个 conda/venv 环境，跑成一个本地 HTTP 微服务，
# 主进程（alt_aligners.py 里的 NeMoForcedAligner）只通过 HTTP 调用它，
# 不在 .mfa_env 里 import nemo。
#
# 用法：
#   conda create -n nemo_env python=3.10 -y
#   conda activate nemo_env
#   pip install "nemo_toolkit[asr]>=2.7.0,<2.8.0"
#   python nemo_server.py
#
# 默认监听 127.0.0.1:5002（5001 已被 qwen3_server.py 占用）。
from __future__ import annotations

from flask import Flask, request, jsonify
from pathlib import Path
import os
import sys
import time
import logging
import threading
import subprocess
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 项目目录现在直接是 backend 目录
BACKEND_DIR = Path(__file__).resolve().parent

# HuggingFace Hub 模型缓存（如 nvidia/stt_zh_citrinet_1024_gamma_0_25、
# nvidia/parakeet-tdt_ctc-0.6b-ja）——独立复用一份缓存目录，不与
# qwen3_server.py 的 HF 缓存共享，避免两个进程同时写同一个 hub 缓存
# 目录产生竞态。
CACHE_DIR = BACKEND_DIR / "models" / "nemo_hf_cache"
HUB_CACHE_DIR = CACHE_DIR / "hub"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
HUB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# NGC pretrained_name（如 stt_en_fastconformer_hybrid_large_pc）的缓存目录
NEMO_CACHE_DIR = BACKEND_DIR / "models" / "nemo_cache"
NEMO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 必须在 import nemo 之前设置
os.environ["HF_HOME"] = str(CACHE_DIR)
os.environ["HF_HUB_CACHE"] = str(HUB_CACHE_DIR)
os.environ["NEMO_CACHE_DIR"] = str(NEMO_CACHE_DIR)

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
try:
    from app_settings import apply_console_visibility as _apply_console_visibility
    _apply_console_visibility()
except Exception as _console_err:
    logger.warning(f"⚠️  设置控制台窗口显示状态失败（不影响服务本身运行）: {_console_err}")

logger.info(f"HF_HOME = {os.environ.get('HF_HOME')}")
logger.info(f"HF_HUB_CACHE = {os.environ.get('HF_HUB_CACHE')}")
logger.info(f"NEMO_CACHE_DIR = {os.environ.get('NEMO_CACHE_DIR')}")
logger.info(f"HF_HUB_OFFLINE = {os.environ.get('HF_HUB_OFFLINE')}")
logger.info(f"HF_ENDPOINT = {os.environ.get('HF_ENDPOINT', 'Official (Not Set)')}")

# ── 各语言默认模型 ───────────────────────────────────────────────────────
# 只收录满足 NFA 限制的官方 checkpoint：纯 CTC 模型，或 CTC 模式下的
# Hybrid CTC-Transducer 模型。纯 Transducer/TDT 模型不能直接用于强制对齐
# （NVIDIA 官方文档明确说明）。
LANGUAGE_MODELS: Dict[str, str] = {
    "en": "stt_en_fastconformer_hybrid_large_pc",       # NGC, Hybrid CTC+RNNT
    "zh": "nvidia/stt_zh_citrinet_1024_gamma_0_25",     # HF Hub, 纯 CTC（字符级）
    "ja": "nvidia/parakeet-tdt_ctc-0.6b-ja",             # HF Hub, Hybrid TDT+CTC
}
# 没有官方 CTC/Hybrid-CTC checkpoint 的语言：不提供默认模型，可通过请求体
# 的 "model" 字段，或环境变量 NEMO_FA_MODEL_{LANG} 自行指定。
_NO_DEFAULT_MODEL_LANGS = frozenset({"ko", "yue"})

_models: Dict[str, Any] = {}          # model_name -> 已加载的 NeMo 模型实例
_model_lock = threading.Lock()

# 当前 HTTP server 实例（在 __main__ 里用 werkzeug.serving.make_server 创建），
# /restart 需要拿到它才能在重启前"干净地"关闭监听端口，见 restart() 里的说明。
_httpd = None
_model_device: str = "auto"           # 记录当前所有已加载模型使用的 device_override


def _pick_device(device_override: str = "auto") -> str:
    """
    与 qwen3_server.py 的 _pick_device_and_dtype 同样的探测逻辑，但 NeMo
    模型走 fp32/AMP 自动管理，这里只需要决定 "cpu" 还是 "cuda"。
    """
    import torch

    if device_override == "cpu":
        return "cpu"
    if not torch.cuda.is_available():
        if device_override == "cuda":
            logger.warning("⚠️  请求 CUDA 但未检测到可用 GPU，回退到 CPU")
        return "cpu"
    try:
        torch.zeros(1, device="cuda")
    except Exception as e:
        logger.warning(f"⚠️  CUDA 初始化失败（{e}），回退到 CPU")
        return "cpu"
    return "cuda"


def _resolve_model_name(language: str, model_override: str = "") -> str:
    """按优先级解析本次请求使用的模型名：请求体 > 环境变量 > 内置默认表。"""
    int_lang = (language or "en").strip().lower()
    # 兼容 ISO 639-2/3 三字码（cmn/eng/jpn/...）传入时的归一化，与
    # alt_aligners.py 中 _normalize_lang() 的映射保持一致
    _alias = {"cmn": "zh", "eng": "en", "jpn": "ja", "kor": "ko"}
    int_lang = _alias.get(int_lang, int_lang)

    if model_override:
        return model_override

    env_key = f"NEMO_FA_MODEL_{int_lang.upper()}"
    env_val = os.environ.get(env_key, "").strip()
    if env_val:
        logger.info(f"使用环境变量 {env_key}={env_val}")
        return env_val

    model_name = LANGUAGE_MODELS.get(int_lang)
    if not model_name:
        if int_lang in _NO_DEFAULT_MODEL_LANGS:
            raise ValueError(
                f"NeMo Forced Aligner 暂无语言 '{int_lang}' 的官方 CTC/Hybrid-CTC "
                f"模型。可在请求体传 'model' 字段指定自有模型，或设置环境变量 "
                f"NEMO_FA_MODEL_{int_lang.upper()}。"
            )
        logger.warning(f"语言 '{int_lang}' 不在内置表中，回退英语模型")
        return LANGUAGE_MODELS["en"]
    return model_name


def _is_cuda_oom_or_env_error(exc: Exception) -> bool:
    """
    与 alt_aligners.py 里同名函数用途一致（NeMo 这里是独立进程，不能直接
    import 主进程模块，因此各自维护一份）：判断异常是否属于"显存不足"
    或"CUDA 环境本身有问题"（Toolkit 缺失/驱动不匹配/无法初始化 CUDA
    context 等）——这两类问题都应该触发"整体切换到 CPU 重试"，而不是把
    原始报错直接抛给用户。
    """
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


def load_model(model_name: str, device_override: str = "auto"):
    """
    惰性加载并缓存 NeMo ASR 模型，按 (model_name, device) 缓存。

    显存不足 / CUDA 环境异常自动降级：加载阶段如果命中 GPU 相关错误，
    自动改在 CPU 上重新加载一次并以 "{model_name}@cpu" 为 key 缓存，
    不会把原始 CUDA 报错直接抛给调用方（align() 路由）。NeMo Forced
    Aligner 官方接口同样没有可调的 batch_size（每次请求都是单条音频
    单次前向，参见 _get_log_probs），因此这里与 Qwen3-ForcedAligner
    一致：能做的唯一有效降级就是整体切到 CPU；qwen3_batch_size 设置
    值目前只在下面的警告日志里作为参考展示。
    """
    global _model_device
    cache_key = f"{model_name}@{device_override}"

    with _model_lock:
        if cache_key in _models:
            return _models[cache_key]

        logger.info(f"正在加载 NeMo 模型: {model_name} (device={device_override}) ...")
        device = _pick_device(device_override)

        import nemo.collections.asr as nemo_asr
        import torch

        # 严格遵守 app_settings 带来的 HF_HUB_OFFLINE 设置，不再强行突破限制
        try:
            model = nemo_asr.models.ASRModel.from_pretrained(
                model_name=model_name,
                map_location=device,
            )
        except Exception as e:
            if device == "cpu" or not _is_cuda_oom_or_env_error(e):
                raise
            logger.warning(
                f"⚠️  在 {device} 上加载 NeMo 模型失败（{e}），自动切换到 CPU 重新加载..."
            )
            try:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            device = "cpu"
            cache_key = f"{model_name}@cpu"
            if cache_key in _models:
                return _models[cache_key]
            model = nemo_asr.models.ASRModel.from_pretrained(
                model_name=model_name,
                map_location=device,
            )

        # Hybrid CTC-Transducer 模型默认解码器是 RNNT/TDT，NFA 要求强制
        # 切到 CTC 模式才能拿到逐帧 log-probs。纯 CTC 模型没有这个方法。
        if hasattr(model, "change_decoding_strategy"):
            try:
                model.change_decoding_strategy(decoder_type="ctc")
                logger.info("Hybrid 模型已切换至 CTC 解码模式")
            except Exception as switch_err:
                logger.debug(f"change_decoding_strategy 跳过: {switch_err}")

        model = model.to(device)
        model.eval()

        try:
            from nemo.utils import logging as nemo_logging
            nemo_logging.setLevel(logging.WARNING)
        except Exception:
            pass

        # 缓存值本身连同"实际生效设备"一起存，避免调用方（/align 路由）
        # 事后再独立调用一次 _pick_device(device_override) 重新推断——
        # 那样在这里发生了 CUDA→CPU 自动降级时，调用方推断出的设备会与
        # 模型实际所在设备不一致，导致后续张量搬运到错误的 device 上。
        _models[cache_key] = (model, device)
        _model_device = device_override
        logger.info(f"✅ NeMo 模型加载成功: {model_name} → {device}")
        return model, device


# ── CTC 辅助函数（与 alt_aligners.py 中曾经的进程内实现逻辑一致，现在搬到
#    这个独立进程里执行）────────────────────────────────────────────────
def _get_blank_id(model) -> int:
    decoder = getattr(model, "decoder", None)
    vocab = getattr(decoder, "vocabulary", None)
    if vocab is not None:
        return len(vocab)
    try:
        return int(model.decoder.num_classes_with_blank) - 1
    except Exception:
        return 0


def _tokenize(model, text: str) -> Tuple[List[int], List[str]]:
    tokenizer = getattr(model, "tokenizer", None)
    if tokenizer is not None and hasattr(tokenizer, "text_to_ids"):
        ids = tokenizer.text_to_ids(text)
        if ids:
            texts: List[str] = []
            if hasattr(tokenizer, "ids_to_tokens"):
                try:
                    texts = [str(t) for t in tokenizer.ids_to_tokens(ids)]
                except Exception:
                    texts = []
            if len(texts) != len(ids) and hasattr(tokenizer, "id_to_piece"):
                try:
                    texts = [str(tokenizer.id_to_piece(i)) for i in ids]
                except Exception:
                    texts = []
            if len(texts) != len(ids):
                texts = list(text)[: len(ids)] + [""] * max(0, len(ids) - len(text))
            return list(ids), texts

    decoder = getattr(model, "decoder", None)
    vocab = getattr(decoder, "vocabulary", None)
    if vocab:
        vocab_idx = {ch: i for i, ch in enumerate(vocab)}
        ids, texts = [], []
        for ch in text:
            idx = vocab_idx.get(ch)
            if idx is None and ch == " ":
                idx = vocab_idx.get("<space>")
            if idx is not None:
                ids.append(idx)
                texts.append(ch)
        return ids, texts

    return [], []


def _get_log_probs(model, audio_path: str, device: str) -> Tuple["Any", int, float]:
    import torch
    import soundfile as sf

    audio_np, sr = sf.read(audio_path, always_2d=False)
    if getattr(audio_np, "ndim", 1) > 1:
        audio_np = audio_np.mean(axis=1)
    audio_np = audio_np.astype("float32")

    if sr != 16000:
        import torchaudio
        t = torch.from_numpy(audio_np).unsqueeze(0)
        audio_np = torchaudio.functional.resample(t, orig_freq=sr, new_freq=16000).squeeze(0).numpy()
        sr = 16000

    audio_sec = len(audio_np) / sr
    audio_tensor = torch.from_numpy(audio_np).unsqueeze(0).to(device)
    audio_len = torch.tensor([audio_tensor.shape[1]], dtype=torch.long, device=device)

    with torch.no_grad():
        try:
            log_probs, enc_len, _ = model(
                input_signal=audio_tensor, input_signal_length=audio_len,
            )
        except TypeError:
            log_probs, enc_len, _ = model(audio_tensor, audio_len)

    T = int(enc_len[0].item())
    lp = log_probs[0, :T, :].detach().to("cpu").float()
    return lp, T, audio_sec


def _merge_bpe_to_words(
    token_entries: List[Tuple[float, float, str]],
) -> List[Tuple[float, float, str]]:
    words: List[Tuple[float, float, str]] = []
    cur_start: Optional[float] = None
    cur_end: Optional[float] = None
    cur_text = ""

    for s, e, tok in token_entries:
        is_word_start = tok.startswith("▁") or tok.startswith(" ") or not cur_text
        clean_tok = tok.lstrip("▁ ")
        if not clean_tok:
            continue
        if is_word_start:
            if cur_text:
                words.append((cur_start, cur_end, cur_text))
            cur_start, cur_end, cur_text = s, e, clean_tok
        else:
            cur_text += clean_tok
            cur_end = e

    if cur_text:
        words.append((cur_start, cur_end, cur_text))
    return words


@app.get("/")
def health():
    return jsonify(
        {
            "success": True,
            "message": "NeMo Forced Aligner service is running",
            "models_loaded": list(_models.keys()),
            "language_models": LANGUAGE_MODELS,
        }
    )


@app.post("/restart")
def restart():
    """
    优雅自重启，让设置页面保存的模型下载配置立刻生效。已加载到显存/内存
    里的 NeMo 模型会随进程重建一起释放，重启后按需重新惰性加载，属于
    预期行为。

    【重要】这里不再使用 os.execv 原地重建进程（与 qwen3_server.py 同步
    修复，问题和原因完全一致）。

    之前是 os.execv(python, [python] + sys.argv)：Windows 没有真正的
    exec()，是 CRT 用 _spawnve(P_OVERLAY, ...) 模拟出来的，而且是从
    _delayed_restart 这个后台线程里调用、主线程还阻塞在 accept 循环里。
    第一次重启"凑巧"能成功，但旧进程监听 5002 端口的 socket 句柄等状态
    没有被干净释放，第二次再触发 /restart 时新进程 bind 端口失败，看起来
    就是"进程直接消失了"，只能重新打开整个启动器——这就是"重启一次没
    问题，重启第二次以上就必须重新打开应用"的根因。

    新做法改成"先干净关闭、再拉起全新独立进程"：
      1) 显式调用 _httpd.shutdown() + server_close()，确保 5002 端口被
         完全释放；
      2) 端口释放后用 subprocess.Popen 启动全新 python 进程，不继承旧
         进程任何多余的线程/句柄状态，此时端口已空闲，一定能 bind 成功；
      3) 不传 creationflags，新进程默认继承当前控制台窗口，日志依然打印
         在同一个窗口里；
      4) 最后 os._exit(0) 立即结束旧进程。
    无论重启多少次，每次都是确定性的"干净关端口 → 起新进程"，不会有
    状态累积。
    """
    def _delayed_restart():
        time.sleep(0.5)
        logger.info("⟳ 收到重启请求，正在重启 nemo_server.py 进程以应用最新设置...")

        global _httpd
        try:
            if _httpd is not None:
                _httpd.shutdown()       # 停止 serve_forever 循环
                _httpd.server_close()   # 真正释放 5002 端口
                logger.info("✓ 已释放端口 5002，准备拉起新进程")
        except Exception as e:
            logger.warning(f"关闭旧 HTTP server 时出现异常（继续重启流程）: {e}")

        python = sys.executable
        try:
            subprocess.Popen([python] + sys.argv, close_fds=True)
        except Exception as e:
            logger.error(f"启动新进程失败: {e}", exc_info=True)

        os._exit(0)

    threading.Thread(target=_delayed_restart, daemon=True).start()
    return jsonify({"success": True, "message": "NeMo Forced Aligner 服务正在重启..."})


@app.post("/align")
def align():
    """
    请求体:
      {
        "audio": "本机绝对路径",
        "text": "参考文本（必填，NFA 是强制对齐，不做纯 ASR）",
        "language": "en" / "zh" / "ja" / ...,
        "model": "可选，覆盖默认模型名（NGC 名 或 'nvidia/xxx' HF 名）",
        "device": "auto" | "cpu" | "cuda",
        "batch_size": "可选，int，仅作为显存不足自动降级重试时的参考值
                       记录到日志——NeMo Forced Aligner 的推理本身是单条
                       音频单次前向（见 _get_log_probs），没有真正意义上
                       可调的批大小；未提供时不影响任何行为。"
      }

    返回:
      {
        "success": true,
        "token_entries": [[start_sec, end_sec, token_text], ...],
        "model": "实际使用的模型名",
        "audio_duration_sec": ...
      }
    客户端（alt_aligners.py 的 NeMoForcedAligner）拿到 token_entries 后，
    自己做英语 BPE 词合并 + _word_entries_to_lab() 生成最终 LAB，
    与 Qwen3ASRAligner 处理 segments 的方式一致。
    """
    data = request.get_json(force=True) or {}

    audio_path = str(data.get("audio") or "")
    text = (data.get("text") or "").strip()
    language = data.get("language") or "en"
    model_override = (data.get("model") or "").strip()
    device_override = data.get("device", "auto")
    if device_override not in ("auto", "cpu", "cuda"):
        device_override = "auto"
    try:
        batch_size_hint = max(1, int(data.get("batch_size", 8)))
    except (TypeError, ValueError):
        batch_size_hint = 8

    if not audio_path or not Path(audio_path).exists():
        return jsonify({"success": False, "error": "音频文件不存在或未提供 audio 参数"}), 400
    if not text:
        return jsonify({"success": False, "error": "NeMo Forced Aligner 需要提供参考文本"}), 400

    try:
        model_name = _resolve_model_name(language, model_override)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    try:
        model, device = load_model(model_name, device_override)
    except Exception as e:
        logger.error(f"模型加载失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": f"模型加载失败: {e}"}), 500

    try:
        import torch
        import torchaudio

        # 【显存不足自动降级】_get_log_probs() 是单条音频单次前向，没有
        # batch_size 可调（与 Qwen3-ForcedAligner 同样的限制，详见
        # alt_aligners.py 里 Qwen3ForcedAligner._align_single_chunk() 的
        # 说明）。命中 GPU 相关错误时，整体重新在 CPU 上加载一次模型并
        # 重试，而不是把原始 CUDA 报错直接返回给前端。
        try:
            log_probs, T, audio_sec = _get_log_probs(model, audio_path, device)
        except Exception as e:
            if device == "cpu" or not _is_cuda_oom_or_env_error(e):
                raise
            logger.warning(
                f"[NeMo-FA] GPU 推理失败（{e}），自动切换到 CPU 重新加载模型并重试"
                f"（客户端传入的参考批大小 batch_size={batch_size_hint}，"
                "但 NeMo Forced Aligner 单次前向没有批可降，"
                "此处直接整体切换运行设备）..."
            )
            try:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            model, device = load_model(model_name, "cpu")
            log_probs, T, audio_sec = _get_log_probs(model, audio_path, device)

        if T == 0:
            return jsonify({"success": False, "error": "模型未返回任何编码帧，请检查音频文件"}), 500

        token_ids, token_texts = _tokenize(model, text)
        if not token_ids:
            return jsonify({
                "success": False,
                "error": f"文本 tokenization 结果为空，请确认参考文本与模型语言匹配（当前模型: {model_name}）",
            }), 400

        blank_id = _get_blank_id(model)
        frame_sec = audio_sec / T

        logger.info(
            f"[NeMo-FA] 对齐: T={T} frames, {len(token_ids)} tokens, "
            f"blank_id={blank_id}, frame={frame_sec * 1000:.1f}ms, model={model_name}"
        )

        emission = log_probs.unsqueeze(0)
        targets = torch.tensor(token_ids, dtype=torch.long).unsqueeze(0)

        spans = None
        try:
            aligned, scores = torchaudio.functional.forced_align(emission, targets, blank=blank_id)
            spans = torchaudio.functional.merge_tokens(aligned[0], scores[0], blank=blank_id)
        except Exception as fa_err:
            logger.warning(f"torchaudio.forced_align 失败: {fa_err}，回退到均匀时间分配")

        if spans is not None:
            # 【修正】上一版补丁假设 spans 里会混入 token==blank_id 的独立
            # blank span，并据此做"向左合并"。但按 torchaudio 官方实现
            # （torchaudio/functional/_alignment.py::merge_tokens()）：
            #     spans = [TokenSpan(token=token, start=start, end=end, ...)
            #              for start, end in zip(changes_wo_blank[:-1], changes_wo_blank[1:])
            #              if (token := tokens[start]) != blank]
            # blank token 在这一步就已经被无条件剔除，merge_tokens() 返回的
            # spans 列表里**根本不存在** token==blank_id 的条目。也就是说
            # `if is_blank_span:` 分支永远不会被命中，等于没有修复任何东西
            # ——这正是用户反馈"仍然没有合并"的真实原因。
            #
            # blank 真正的"藏身之处"不是某个独立的 span，而是相邻两个真实
            # span 在帧轴上的不连续：spans[i+1].start 帧号会大于
            # spans[i].end 帧号，中间那段缺口正是被 merge_tokens() 直接
            # 丢弃的 blank 帧。原先的写法对此视而不见——entry 的
            # t_start/t_end 严格等于 span.start/span.end 换算的秒数，于是
            # 这段被丢弃的帧区间，就变成了 token_entries 序列里一段
            # "无主"的真实时间空隙。这段空隙在数值上没有消失（两侧时间戳
            # 仍然对得上原始音频），但下游 _fill_silences_lab() 一旦扫到
            # 任何 ≥ 50ms 的空隙就会在那里插入一条 SIL——而 NeMo citrinet
            # 等模型的单帧时长（本例中约 79ms）本身就已经超过这个 50ms
            # 阈值，导致几乎每一个相邻字符之间的正常帧量化间隙，都被
            # 错误地当成"真实停顿"转成了 SIL，SVP 里因此出现"每个字之间
            # 都被强行隔开"的现象。
            #
            # 正确修复：按 span 在帧轴上的真实位置检测相邻 span 间的帧缺口，
            # 一旦发现就把这段时长整体并入前一个真实 token 的结尾（向左
            # 合并，而不是放任它变成游离空隙）。音频开头第一个 token 之前
            # 的空隙不在此处处理，交给 _fill_silences_lab() 的"首条目"
            # 判断逻辑负责（开头静音本来就该是 SIL）。
            token_entries: List[Tuple[float, float, str]] = []
            prev_span_end_frame: Optional[float] = None
            for i, span in enumerate(spans):
                if i >= len(token_texts):
                    break
                tok_txt = token_texts[i]

                # 向左合并：当前 span 起始帧若晚于上一个 span 的结束帧，
                # 中间这段就是被 merge_tokens() 抹掉的 blank 帧，整体
                # 计入"上一个已生成的 token_entries 条目"的结尾。
                if prev_span_end_frame is not None and span.start > prev_span_end_frame:
                    if token_entries:
                        merge_end = min(span.start * frame_sec, audio_sec)
                        prev_s, prev_e, prev_tok = token_entries[-1]
                        token_entries[-1] = (prev_s, max(prev_e, merge_end), prev_tok)

                t_start = span.start * frame_sec
                t_end = min(span.end * frame_sec, audio_sec)
                if t_end > t_start and tok_txt:
                    token_entries.append((t_start, t_end, tok_txt))

                prev_span_end_frame = span.end
        else:
            spoken = [t for t in token_texts if (t or "").strip()]
            if spoken:
                dur = audio_sec / len(spoken)
                token_entries = [
                    (i * dur, min((i + 1) * dur, audio_sec), t)
                    for i, t in enumerate(spoken)
                ]
            else:
                token_entries = []

        if not token_entries:
            return jsonify({"success": False, "error": "强制对齐未产生任何时间戳条目"}), 500

        int_lang = {"cmn": "zh", "eng": "en", "jpn": "ja", "kor": "ko"}.get(
            (language or "en").strip().lower(), (language or "en").strip().lower()
        )
        if int_lang == "en":
            has_bpe_marker = any(
                (t.startswith("▁") or t.startswith(" ")) for _, _, t in token_entries
            )
            if has_bpe_marker:
                token_entries = _merge_bpe_to_words(token_entries)

        return jsonify(
            {
                "success": True,
                "token_entries": [[s, e, t] for s, e, t in token_entries],
                "model": model_name,
                "audio_duration_sec": audio_sec,
            }
        )

    except Exception as e:
        logger.error(f"对齐失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    # 生产环境建议改成 waitress / gevent / gunicorn，与 qwen3_server.py 一致
    #
    # 这里不用 app.run(...)，改用 werkzeug.serving.make_server(...) 拿到
    # 底层 server 对象存进 _httpd —— /restart 需要它来在重启前调用
    # shutdown() + server_close() 干净地释放端口，见 restart() 里的说明。
    from werkzeug.serving import make_server

    _httpd = make_server("127.0.0.1", 5002, app)
    logger.info("🚀 NeMo Forced Aligner service listening on http://127.0.0.1:5002")
    _httpd.serve_forever()
