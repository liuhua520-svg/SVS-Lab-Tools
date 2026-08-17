# qwen3_server.py
from __future__ import annotations

from flask import Flask, request, jsonify
from pathlib import Path
import os
import sys
import time
import logging
import threading
import subprocess
from typing import Any, Dict, List, Optional

# 【2026-08 变更，修复"直接用 python 启动本脚本时，命令行只显示启动阶段
# 那几行日志，之后处理请求产生的日志只在日志文件里能看到，命令行窗口不再
# 更新"】原因和修法与 app.py 头部同一处改动完全一致：Python 判断
# sys.stdout 是否连着真正的交互式终端来决定用行缓冲还是全缓冲，这个判断
# 在 Windows 上不总是可靠，退化成全缓冲后，日志会攒在内部缓冲区里迟迟不
# 刷新到命令行窗口（但 logging 模块如果配了 FileHandler，每次 emit()
# 都会立刻 flush，不受影响，所以日志文件里反而更全）。
# 用 reconfigure(line_buffering=True) 强制改成行缓冲，不再依赖自动判断。
# --noconsole/由 launcher.py 以 CREATE_NO_WINDOW 拉起时 sys.stdout/
# sys.stderr 可能是 None，判空后跳过，不影响正常发布环境下的运行。
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

# 缓存固定到当前应用内
CACHE_DIR = BACKEND_DIR / "models" / "hf_cache"
HUB_CACHE_DIR = CACHE_DIR / "hub"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
HUB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 必须在导入 qwen_asr / transformers 相关包之前设置
os.environ["HF_HOME"] = str(CACHE_DIR)
os.environ["HF_HUB_CACHE"] = str(HUB_CACHE_DIR)

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
# 就没有控制台窗口，这里调用是无操作（GetConsoleWindow() 返回空句柄，
# 函数直接返回 False，不算错误）；只有直接用 `python qwen3_server.py`
# 在真实终端里调试时才会真的隐藏/显示那个终端窗口。
try:
    from app_settings import apply_console_visibility as _apply_console_visibility
    _apply_console_visibility()
except Exception as _console_err:
    logger.warning(f"⚠️  设置控制台窗口显示状态失败（不影响服务本身运行）: {_console_err}")

logger.info(f"HF_HOME = {os.environ.get('HF_HOME')}")
logger.info(f"HF_HUB_CACHE = {os.environ.get('HF_HUB_CACHE')}")
logger.info(f"HF_HUB_OFFLINE = {os.environ.get('HF_HUB_OFFLINE')}")
logger.info(f"HF_ENDPOINT = {os.environ.get('HF_ENDPOINT', 'Official (Not Set)')}")

MODEL_ID = "Qwen/Qwen3-ASR-1.7B"
FORCED_ALIGNER_ID = "Qwen/Qwen3-ForcedAligner-0.6B"

_model = None
_model_lock = threading.Lock()

# ═════════════════════════════════════════════════════════════════════════
# 独立的 Qwen3-ForcedAligner（强制对齐，已知文本→时间戳）模型槽位。
#
# 与上面的 _model（Qwen3ASRModel，语音→文本+时间戳）完全独立维护：
#   - _model 内部虽然也挂了一个 forced_aligner 子组件（见 load_model()
#     里 kwargs["forced_aligner"] = FORCED_ALIGNER_ID），但那是
#     Qwen3ASRModel.transcribe() 在"转写"过程中内部调用的，接口是
#     "音频→文字+时间戳"，不接受调用方传入参考文本；
#   - 这里的 _fa_model 是 qwen_asr.Qwen3ForcedAligner 本身，接口是
#     .align(audio=, text=, language=) → 已知文本对齐到音频，这是
#     alt_aligners.py 里 Qwen3ForcedAligner（Qwen3-FA 客户端）真正需要
#     的能力，因此必须单独 from_pretrained 加载，不能复用 _model。
# 两个模型可以同时常驻显存/内存（各自独立缓存，互不清空对方），也可以
# 只按需加载其中一个——具体取决于用户实际用到了哪个功能。
# ═════════════════════════════════════════════════════════════════════════
_fa_model = None
_fa_model_lock = threading.Lock()
_fa_model_device: str = "auto"

# 当前 HTTP server 实例（在 __main__ 里用 werkzeug.serving.make_server 创建），
# /restart 需要拿到它才能在重启前"干净地"关闭监听端口，见 restart() 里的说明。
_httpd = None


def _pick_device_and_dtype(device_override: str = "auto"):
    """
    根据设备参数和实际 GPU 架构选择运行设备与数据类型。

    dtype 选择策略（避免在不支持的 GPU 上使用错误精度）：
      - bfloat16：需要 Ampere (CC ≥ 8.0，RTX 30xx / A100+)
      - float16 ：Pascal (CC 6.x) / Volta (CC 7.0) / Turing (CC 7.5) 均支持
      - float32 ：CPU 或无法确定 GPU 能力时的保底选项

    P106-100 (Pascal, CC 6.1) → float16（不是 bfloat16！）
    """
    import torch

    # 强制 CPU
    if device_override == "cpu":
        return "cpu", torch.float32

    if not torch.cuda.is_available():
        if device_override == "cuda":
            logger.warning("⚠️  请求 CUDA 但未检测到可用 GPU，回退到 CPU")
        return "cpu", torch.float32

    # CUDA smoke-test：防止 CPU-only PyTorch 版本误报 is_available()
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
            # Ampere / Ada Lovelace / Hopper → bfloat16（训练稳定性更好）
            dtype = torch.bfloat16
        elif cc_major >= 6:
            # Pascal / Volta / Turing → float16（bfloat16 硬件不支持）
            dtype = torch.float16
        else:
            # 超旧卡保底
            dtype = torch.float32

        logger.info(f"自动选择 dtype: {dtype}")
        return device, dtype
    except Exception as e:
        logger.warning(f"⚠️  GPU 能力查询失败（{e}），使用 float32 保底")
        return device, torch.float32


def _normalize_time_stamps(value: Any) -> List[List[Optional[float]]]:
    """
    尽量把不同返回形态统一成 [[start, end], ...]
    """
    if value is None:
        return []

    # 形如 [(s, e), (s, e)]
    if isinstance(value, list) and value and isinstance(value[0], (list, tuple)):
        out: List[List[Optional[float]]] = []
        for item in value:
            if len(item) >= 2:
                out.append([item[0], item[1]])
        return out

    # 形如 {"start": s, "end": e}
    if isinstance(value, dict):
        s = value.get("start")
        e = value.get("end")
        if s is not None or e is not None:
            return [[s, e]]
        return []

    return []


def _normalize_segments(result: Any) -> List[Dict[str, Any]]:
    """
    将 qwen_asr 的返回结果统一成客户端容易消费的格式：
    [
      {
        "language": "...",
        "text": "...",
        "time_stamps": [[s, e], ...]
      }
    ]
    """
    segments: List[Dict[str, Any]] = []

    if result is None:
        return segments

    # 1) 如果是 list
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                text = (item.get("text") or "").strip()
                lang = item.get("language")
                ts = item.get("time_stamps")
                if ts is None:
                    ts = item.get("timestamp")
                segments.append(
                    {
                        "language": lang,
                        "text": text,
                        "time_stamps": _normalize_time_stamps(ts),
                    }
                )
            else:
                # 兼容对象形式
                text = (getattr(item, "text", "") or "").strip()
                lang = getattr(item, "language", None)
                ts = getattr(item, "time_stamps", None)
                if ts is None:
                    ts = getattr(item, "timestamp", None)
                segments.append(
                    {
                        "language": lang,
                        "text": text,
                        "time_stamps": _normalize_time_stamps(ts),
                    }
                )
        return segments

    # 2) 如果是 dict
    if isinstance(result, dict):
        # 常见情况：直接给一个整体结果
        text = (result.get("text") or result.get("raw_text") or "").strip()
        lang = result.get("language")
        ts = result.get("time_stamps")
        if ts is None:
            ts = result.get("timestamp")

        # 可能本身就带 chunks / segments
        if "segments" in result and isinstance(result["segments"], list):
            for seg in result["segments"]:
                if isinstance(seg, dict):
                    segments.append(
                        {
                            "language": seg.get("language", lang),
                            "text": (seg.get("text") or "").strip(),
                            "time_stamps": _normalize_time_stamps(
                                seg.get("time_stamps", seg.get("timestamp"))
                            ),
                        }
                    )
            if segments:
                return segments

        if "chunks" in result and isinstance(result["chunks"], list):
            for ch in result["chunks"]:
                if isinstance(ch, dict):
                    segments.append(
                        {
                            "language": ch.get("language", lang),
                            "text": (ch.get("text") or "").strip(),
                            "time_stamps": _normalize_time_stamps(
                                ch.get("time_stamps", ch.get("timestamp"))
                            ),
                        }
                    )
            if segments:
                return segments

        segments.append(
            {
                "language": lang,
                "text": text,
                "time_stamps": _normalize_time_stamps(ts),
            }
        )
        return segments

    # 3) 其他对象
    text = (getattr(result, "text", "") or "").strip()
    lang = getattr(result, "language", None)
    ts = getattr(result, "time_stamps", None)
    if ts is None:
        ts = getattr(result, "timestamp", None)

    segments.append(
        {
            "language": lang,
            "text": text,
            "time_stamps": _normalize_time_stamps(ts),
        }
    )
    return segments


_model_device: str = "auto"   # 记录当前模型加载时所用的 device_override
_model_batch_size: int = 8    # 记录当前模型加载时所用的 max_inference_batch_size


def _is_cuda_oom_or_env_error(exc: Exception) -> bool:
    """
    与 alt_aligners.py / nemo_server.py 里同名函数用途一致（各进程相互
    独立，无法直接 import，各自维护一份）：判断异常是否属于"显存不足"
    或"CUDA 环境本身有问题"，命中时应该整体切换到 CPU 重试，而不是把
    原始报错直接抛给调用方。
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


def load_model(device_override: str = "auto", batch_size: int = 8):
    """
    惰性加载并缓存 Qwen3-ASR 模型。

    batch_size 透传给 qwen_asr 官方的 max_inference_batch_size 参数
    （限制模型内部单次推理的最大批量；值越小，显存占用峰值越低，见
    https://github.com/QwenLM/Qwen3-ASR 的 Python Package Usage 示例）。
    与 device_override 一样，作为缓存 key 的一部分——batch_size 改变时
    需要重新加载模型（forced_aligner 的初始化参数与 ASR 主模型绑定在
    同一次 from_pretrained 调用里，无法只热更新这一个数值）。
    """
    global _model, _model_device, _model_batch_size

    with _model_lock:
        # 如果已有模型且设备、batch_size 均未变化，直接复用
        if _model is not None and _model_device == device_override and _model_batch_size == batch_size:
            return _model

        # 设备或 batch_size 变化，或首次加载
        if _model is not None:
            logger.info(
                f"配置从 device='{_model_device}' batch_size={_model_batch_size} 变为 "
                f"device='{device_override}' batch_size={batch_size}，重新加载模型..."
            )
            _model = None
            try:
                import torch as _torch_reload
                if _torch_reload.cuda.is_available():
                    _torch_reload.cuda.empty_cache()
            except Exception:
                pass

        logger.info("正在初始化 Qwen3-ASR 服务...")
        device_map, dtype = _pick_device_and_dtype(device_override)
        logger.info(f"使用设备: {device_map}, dtype: {dtype}, batch_size: {batch_size}")

        import torch
        from qwen_asr import Qwen3ASRModel

        def _build(_device_map: str, _dtype, _batch_size: int):
            kwargs = {
                "dtype": _dtype,
                "device_map": _device_map,
                "max_inference_batch_size": _batch_size if _device_map.startswith("cuda") else 1,
                "max_new_tokens": 256,
            }
            # 统一启用 forced aligner，这样客户端更容易拿到时间戳
            kwargs["forced_aligner"] = FORCED_ALIGNER_ID
            kwargs["forced_aligner_kwargs"] = {
                "dtype": _dtype,
                "device_map": _device_map,
            }
            return Qwen3ASRModel.from_pretrained(MODEL_ID, **kwargs)

        try:
            _model = _build(device_map, dtype, batch_size)
            _model_device = device_override
            _model_batch_size = batch_size
            logger.info("✅ Qwen3-ASR 模型加载成功！服务已就绪。")
            return _model
        except Exception as e:
            # 【显存不足 / CUDA 环境异常自动降级】加载阶段本身也可能因为
            # 显存不足或 CUDA Toolkit 缺失/版本不匹配而失败。命中这类错误
            # 且当前不是已经在 CPU 上尝试时，自动整体切换到 CPU 重新加载，
            # 不把原始 CUDA 报错直接抛给 /asr 路由。真正因为 batch_size
            # 过大导致的显存不足，理论上应该先尝试腰斩 batch_size 重试
            # （更快、不需要换设备），这里补上这一层，与 CPU 兜底分两级：
            if device_map.startswith("cuda") and _is_cuda_oom_or_env_error(e) and batch_size > 1:
                half_batch = max(1, batch_size // 2)
                logger.warning(
                    f"⚠️  加载 Qwen3-ASR 失败（{e}），尝试腰斩 "
                    f"max_inference_batch_size: {batch_size} → {half_batch} 重试..."
                )
                try:
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                try:
                    _model = _build(device_map, dtype, half_batch)
                    _model_device = device_override
                    _model_batch_size = half_batch
                    logger.info(
                        f"✅ Qwen3-ASR 模型加载成功（batch_size 降级为 {half_batch}）！服务已就绪。"
                    )
                    return _model
                except Exception as e2:
                    e = e2  # 落入下面的 CPU 兜底分支

            if device_map.startswith("cuda") and _is_cuda_oom_or_env_error(e):
                logger.warning(f"⚠️  在 GPU 上加载 Qwen3-ASR 失败（{e}），自动切换到 CPU 重新加载...")
                try:
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                try:
                    _model = _build("cpu", torch.float32, 1)
                    _model_device = "cpu"
                    _model_batch_size = 1
                    logger.info("✅ Qwen3-ASR 模型加载成功（已回退到 CPU）！服务已就绪。")
                    return _model
                except Exception as e3:
                    logger.error(f"❌ CPU 兜底加载仍然失败: {e3}", exc_info=True)
                    _model = None
                    return None

            logger.error(f"❌ 模型加载失败: {e}", exc_info=True)
            _model = None
            return None


def load_forced_aligner(device_override: str = "auto"):
    """
    惰性加载并缓存独立的 Qwen3-ForcedAligner 模型（.align() 接口，见上方
    _fa_model 说明）。逻辑与 load_model() 对称，但没有 batch_size 这个轴
    ——Qwen3-ForcedAligner 官方接口本身不支持按批调用，每次都是单条音频
    单次前向，这一点与 alt_aligners.py 里 Qwen3ForcedAligner._load_model()
    的注释保持一致（迁移到这里之前，那份注释本身就是这么写的）。
    """
    global _fa_model, _fa_model_device

    with _fa_model_lock:
        if _fa_model is not None and _fa_model_device == device_override:
            return _fa_model

        if _fa_model is not None:
            logger.info(
                f"[Qwen3-FA] 配置从 device='{_fa_model_device}' 变为 "
                f"device='{device_override}'，重新加载模型..."
            )
            _fa_model = None
            try:
                import torch as _torch_reload
                if _torch_reload.cuda.is_available():
                    _torch_reload.cuda.empty_cache()
            except Exception:
                pass

        logger.info("正在初始化 Qwen3-ForcedAligner 服务...")
        device_map, dtype = _pick_device_and_dtype(device_override)
        logger.info(f"[Qwen3-FA] 使用设备: {device_map}, dtype: {dtype}")

        from qwen_asr import Qwen3ForcedAligner as Qwen3FA

        try:
            _fa_model = Qwen3FA.from_pretrained(
                FORCED_ALIGNER_ID, dtype=dtype, device_map=device_map,
            )
            _fa_model_device = device_map
            logger.info("✅ Qwen3-ForcedAligner 模型加载成功！服务已就绪。")
            return _fa_model
        except Exception as e:
            # 与 load_model() 同样的道理：加载阶段本身也可能因为显存不足
            # 或 CUDA 环境问题失败，命中时自动整体切换到 CPU 重新加载。
            if device_map.startswith("cuda") and _is_cuda_oom_or_env_error(e):
                logger.warning(f"⚠️  在 GPU 上加载 Qwen3-ForcedAligner 失败（{e}），自动切换到 CPU 重新加载...")
                try:
                    import torch as _torch_oom
                    if _torch_oom.cuda.is_available():
                        _torch_oom.cuda.empty_cache()
                except Exception:
                    pass
                try:
                    _fa_model = Qwen3FA.from_pretrained(
                        FORCED_ALIGNER_ID, dtype=__import__("torch").float32, device_map="cpu",
                    )
                    _fa_model_device = "cpu"
                    logger.info("✅ Qwen3-ForcedAligner 模型加载成功（已回退到 CPU）！服务已就绪。")
                    return _fa_model
                except Exception as e2:
                    logger.error(f"❌ CPU 兜底加载仍然失败: {e2}", exc_info=True)
                    _fa_model = None
                    return None

            logger.error(f"❌ Qwen3-ForcedAligner 模型加载失败: {e}", exc_info=True)
            _fa_model = None
            return None


@app.get("/")
def health():
    return jsonify(
        {
            "success": True,
            "message": "Qwen3-ASR service is running",
            "model_loaded": _model is not None,
            "model_id": MODEL_ID,
            "device": _model_device if _model is not None else None,
            "batch_size": _model_batch_size if _model is not None else None,
        }
    )


@app.post("/restart")
def restart():
    """
    优雅自重启，让"设置页面"里保存的模型自动更新 / 镜像站配置立刻生效，
    不需要用户手动去关闭再打开这个独立终端窗口。

    【重要】这里不再使用 os.execv 原地重建进程。

    之前的实现是 os.execv(python, [python] + sys.argv)，在 Linux 上确实是
    "原地替换进程镜像、PID 不变"，但 Windows 没有真正的 exec()，Python/CRT
    是用 _spawnve(P_OVERLAY, ...) 模拟出来的，而且这里是从 _delayed_restart
    这个后台线程里调用的（主线程还阻塞在 werkzeug 的 accept 循环里）。
    第一次重启"凑巧"能成功，但旧进程监听 5001 端口的 socket 句柄、模型占用
    的线程等状态并没有被干净地释放/继承，等第二次再触发 /restart 时，新
    进程 bind 5001 端口会失败——而这次失败恰好发生在 execv 覆盖、日志系统
    还没完全恢复的窗口期，看起来就是"进程直接消失了"，只能重新打开整个
    启动器。这正是"重启一次没问题，重启第二次以上就必须重新打开应用"的
    根因。

    新做法改成"先干净关闭、再拉起全新独立进程"，两步都在当前进程仍然存活
    时完成，避免了 execv 的所有坑：
      1) 显式调用 _httpd.shutdown() + server_close()，确保 5001 端口被
         完全释放（而不是寄希望于 execv 帮我们处理句柄）；
      2) 端口释放后，用 subprocess.Popen 启动一个全新的 python 进程
         （同一套解释器 + 同一条命令行），它不继承旧进程任何多余的线程/
         句柄状态，此时端口已空闲，一定能 bind 成功；
      3) 【2026-08 变更】显式传入 stdout=sys.stdout, stderr=sys.stdout：
         launcher.py 正常启动本进程时，sys.stdout/sys.stderr 已经被重定向
         到 logs/qwen3.log 文件（见 launcher.py _spawn()，用的是
         CREATE_NO_WINDOW，不再创建控制台窗口）。之前这里不传
         stdout/stderr，在 Windows 上配合 close_fds=True 意味着"新进程不
         继承任何标准句柄"，会导致重启后的新进程里 print() 和
         logging.StreamHandler(默认写 stderr) 全部失效（往一个无效句柄
         写入），日志从重启那一刻起彻底丢失，且可能在某些 Python/OS 组合
         下直接抛异常。现在显式传当前进程的 stdout/stderr 过去，新进程
         会继续写同一个日志文件，重启前后日志连续不丢失。若是手动用
         `python qwen3_server.py` 在真实终端调试，sys.stdout/stderr 就是
         那个终端的句柄，行为与之前一致（继续打印在同一个窗口里）。
      4) 最后用 os._exit(0) 立即结束旧进程，不等待任何非必要的清理逻辑。
    这样无论重启多少次，每次都是"干净关端口 → 起新进程"的确定性流程，
    不会有状态累积。
    """
    def _delayed_restart():
        time.sleep(0.5)
        logger.info("⟳ 收到重启请求，正在重启 qwen3_server.py 进程以应用最新设置...")

        global _httpd
        try:
            if _httpd is not None:
                _httpd.shutdown()       # 停止 serve_forever 循环
                _httpd.server_close()   # 真正释放 5001 端口
                logger.info("✓ 已释放端口 5001，准备拉起新进程")
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
    return jsonify({"success": True, "message": "Qwen3-ASR 服务正在重启..."})


@app.post("/asr")
def asr():
    data = request.get_json(force=True) or {}

    # 客户端可传 "device": "auto"|"cpu"|"cuda" 控制运行设备
    device_override = data.get("device", "auto")
    if device_override not in ("auto", "cpu", "cuda"):
        device_override = "auto"

    # 客户端可传 "batch_size" 覆盖 max_inference_batch_size（见
    # load_model() 顶部说明）；非法/缺失值回退为 8，与
    # app_settings.DEFAULT_SETTINGS["qwen3_batch_size"] 的默认值一致。
    try:
        batch_size = max(1, int(data.get("batch_size", 8)))
    except (TypeError, ValueError):
        batch_size = 8

    model = load_model(device_override, batch_size)
    if model is None:
        return jsonify({"success": False, "error": "模型未加载"}), 500

    try:
        audio_path = data.get("audio")
        language = data.get("language")
        context = data.get("context", "")

        if not audio_path:
            return jsonify({"success": False, "error": "缺少 audio 参数"}), 400

        audio_path = str(audio_path)
        if not Path(audio_path).exists():
            return jsonify({"success": False, "error": "音频文件不存在"}), 400

        # qwen_asr 官方接口：transcribe
        # return_time_stamps=True 便于客户端构建 LAB
        #
        # 【显存不足自动降级】即使模型加载阶段成功，真正推理时仍可能因为
        # 单条音频过长/内容复杂而 CUDA OOM（尤其在批大小较大、显存本身
        # 紧张的显卡上）。命中时按 load_model() 同样的两级降级策略重试：
        # 先尝试腰斩 max_inference_batch_size 重新加载模型再推理一次，
        # 仍不行则整体切换到 CPU 重新加载再推理一次。
        try:
            result = model.transcribe(
                audio=audio_path,
                language=language,
                context=context,
                return_time_stamps=True,
            )
        except Exception as e:
            if not _is_cuda_oom_or_env_error(e):
                raise
            logger.warning(f"[Qwen3-ASR] 推理失败（{e}），尝试自动降级重试...")
            try:
                import torch as _torch_oom
                if _torch_oom.cuda.is_available():
                    _torch_oom.cuda.empty_cache()
            except Exception:
                pass

            retried = False
            if _model_device != "cpu" and batch_size > 1:
                half_batch = max(1, batch_size // 2)
                model = load_model(device_override, half_batch)
                if model is not None:
                    retried = True
            if not retried or model is None:
                model = load_model("cpu", 1)

            if model is None:
                return jsonify({"success": False, "error": "显存不足自动降级后模型仍加载失败"}), 500

            result = model.transcribe(
                audio=audio_path,
                language=language,
                context=context,
                return_time_stamps=True,
            )

        segments = _normalize_segments(result)
        raw_text = "".join([seg.get("text", "") for seg in segments]).strip()
        
        # ↓↓↓ 新增这两行（实时输出识别结果到命令行）
        logger.info(f"✅ 识别完成 | 设备={_model_device} | batch={_model_batch_size}")
        logger.info(f"📝 识别文字: {raw_text}")
        # ↑↑↑ 新增结束

        return jsonify(
            {
                "success": True,
                "segments": segments,
                "raw_text": raw_text,
                "model_id": MODEL_ID,
            }
        )

    except Exception as e:
        logger.error(f"推理失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.post("/align")
def align():
    """
    独立强制对齐接口：已知文本 + 音频 → 该段音频自身时间轴（从 0 开始）
    上的逐 token 时间戳，供 alt_aligners.py 里的 Qwen3ForcedAligner
    客户端调用（替代此前直接 import qwen_asr 在 .mfa_env 里本地加载模型
    的做法，见该类顶部注释）。

    这里只做"调用模型拿原始结果"这一件事，分句/分块规划、退化区间检测
    与自愈修复、LAB 文本组装等后处理逻辑全部保留在客户端（alt_aligners.py
    的 Qwen3ForcedAligner 类里），不搬过来——那些逻辑不依赖 qwen_asr，
    没有理由拉长每次 HTTP 往返，也避免这个服务进程和客户端进程的后处理
    逻辑出现两份、日后改一处忘改另一处。
    """
    data = request.get_json(force=True) or {}

    device_override = data.get("device", "auto")
    if device_override not in ("auto", "cpu", "cuda"):
        device_override = "auto"

    audio_path = data.get("audio")
    text = data.get("text")
    language = data.get("language")

    if not audio_path:
        return jsonify({"success": False, "error": "缺少 audio 参数"}), 400
    if not text:
        return jsonify({"success": False, "error": "缺少 text 参数"}), 400

    audio_path = str(audio_path)
    if not Path(audio_path).exists():
        return jsonify({"success": False, "error": "音频文件不存在"}), 400

    model = load_forced_aligner(device_override)
    if model is None:
        return jsonify({"success": False, "error": "模型未加载"}), 500

    try:
        # 官方接口：results[0][0].text / start_time / end_time
        #
        # 【显存不足自动降级】与 /asr 同样的道理：加载阶段成功不代表推理
        # 阶段一定成功，命中 CUDA OOM / 环境错误时整体切换到 CPU 重新
        # 加载模型再重试一次。Qwen3-ForcedAligner 官方接口本身没有
        # batch_size 可调（每次都是单条音频单次前向），所以这里只有一级
        # 降级（直接切 CPU），不像 /asr 那样先腰斩批大小再切 CPU。
        try:
            results = model.align(audio=audio_path, text=text, language=language)
        except Exception as e:
            if not _is_cuda_oom_or_env_error(e):
                raise
            logger.warning(f"[Qwen3-FA] 推理失败（{e}），自动切换到 CPU 重新加载并重试...")
            try:
                import torch as _torch_oom
                if _torch_oom.cuda.is_available():
                    _torch_oom.cuda.empty_cache()
            except Exception:
                pass
            model = load_forced_aligner("cpu")
            if model is None:
                return jsonify({"success": False, "error": "显存不足自动降级后模型仍加载失败"}), 500
            results = model.align(audio=audio_path, text=text, language=language)

        items = results[0] if results else []
        entries = [
            {
                "text": getattr(item, "text", "") or "",
                "start_time": float(item.start_time),
                "end_time": float(item.end_time),
            }
            for item in items
        ]

        return jsonify(
            {
                "success": True,
                "entries": entries,
                "model_id": FORCED_ALIGNER_ID,
            }
        )

    except Exception as e:
        logger.error(f"[Qwen3-FA] 对齐失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    # 生产环境建议改成 waitress / gevent / gunicorn
    #
    # 这里不用 app.run(...)，改用 werkzeug.serving.make_server(...) 拿到
    # 底层 server 对象存进 _httpd —— /restart 需要它来在重启前调用
    # shutdown() + server_close() 干净地释放端口，见 restart() 里的说明。
    from werkzeug.serving import make_server

    _httpd = make_server("127.0.0.1", 5001, app)
    logger.info("🚀 Qwen3-ASR service listening on http://127.0.0.1:5001")
    _httpd.serve_forever()