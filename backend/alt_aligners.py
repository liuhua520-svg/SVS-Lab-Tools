# -*- coding: utf-8 -*-
"""
alt_aligners.py — 替代音素对齐后端
支持 WhisperX / Qwen3-ASR-1.7B / Qwen3-ForcedAligner-0.6B 作为 MFA 的替代选项

模型文件路径策略（优先级）：
  1. 环境变量 TSUBAKI_MODELS_DIR
  2. <当前文件所在目录>/models/      → 即 backend/models/
     ├── hf_cache/         HuggingFace 统一缓存 (Qwen3-ASR / Qwen3-FA)
     │   └── hub/
     └── rmvpe/            RMVPE 模型 (已有，路径不变)

标点/静音处理：
  Qwen3 不在对齐输出中输出标点（标点不可发声）。
  本模块在生成 LAB 后自动扫描时间轴间隙，将 ≥ 50ms 的空白补全为 SIL 条目。
  用户无需为标点担心，静音标记由时间间隙自动推断。
"""
from __future__ import annotations

import logging
import os
import time
import unicodedata
import warnings
import requests
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 【修复】必须在本文件任何地方 import speechbrain / qwen_asr 之前，
# 先强制真正执行 librosa/core/audio.py 这个子模块（而不是只 import librosa）。
#
# 原因（很绕，但确认过）：librosa/core/audio.py 在被真正执行的瞬间会跑
#     samplerate = lazy.load("samplerate")
# lazy_loader.load() 内部用 inspect.stack() 检查调用者所在的模块——这一步
# 会触发 Python 标准库 inspect.getmodule()，它会遍历 sys.modules 里的*所有*
# 模块，对每一个都做 hasattr(module, "__file__")。
#
# 而 qwen_asr 在加载 Qwen3-ForcedAligner 模型时会连带 import speechbrain；
# speechbrain 会把一些可选子模块（如 speechbrain.integrations.nlp，用于
# flair 词向量，本项目完全用不到）注册成"懒加载占位对象"塞进 sys.modules。
# 这个占位对象只要被 hasattr() 摸一下 __file__，就会触发它真正尝试
# import（最终因为没装 flair 而失败），从而把上面那次纯粹为了内部记账
# 用途的 inspect.getmodule() 调用搞炸，报错信息看起来跟 librosa/Qwen3-FA
# 本身毫无关系（"Lazy import of LazyModule(...integrations.nlp...) failed"）。
#
# 【关键】librosa 顶层包自己也用 lazy_loader 做"包级别懒加载"：单纯
# `import librosa` 并不会真的执行 core/audio.py，只是注册了一个代理，
# 真正执行要等到第一次有人调用 librosa.load(...) 才触发——而那次往往
# 已经在 speechbrain 被 import 之后了，雷照样会踩中。必须用下面这种
# 显式到子模块的 import 写法，绕开 librosa 自己的包级懒加载代理，
# 强制 Python 的 import 机制立刻、真实地执行 core/audio.py。
#
# 不要删除这一行，也不要把它移到文件后面或挪进某个函数里。
import librosa.core.audio  # noqa: F401

logger = logging.getLogger(__name__)

# 屏蔽 pyannote.audio 在 torchcodec DLL 找不到时输出的 UserWarning
# （非致命：pyannote 会自动回退到其他解码后端）
warnings.filterwarnings(
    "ignore",
    message=r".*torchcodec.*",
    category=UserWarning,
)
warnings.filterwarnings(
    "ignore",
    message=r".*torchaudio\._backend\.list_audio_backends.*",
    category=UserWarning,
)


# ═════════════════════════════════════════════════════════════════════════════
# 0. CUDA 可用性探针
# ═════════════════════════════════════════════════════════════════════════════

def _torch_cuda_usable() -> bool:
    """
    判断当前 PyTorch 是否真正可以使用 CUDA。

    torch.cuda.is_available() 在 NVIDIA 驱动存在时可能返回 True，
    即便 PyTorch 本身是 CPU-only 版本（未编译 CUDA 支持）。
    真正分配一个 CUDA tensor 才能暴露这一差异。
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return False
        # 实际触发 CUDA 初始化，CPU-only 版本在此抛 AssertionError
        torch.zeros(1, device="cuda")
        return True
    except Exception:
        return False


def _is_cuda_oom_or_env_error(exc: Exception) -> bool:
    """
    判断一个异常是否属于"显存不足"或"CUDA 环境本身有问题"（例如未正确
    安装 CUDA Toolkit / 驱动版本不匹配 / PyTorch 是 CPU-only 版本却被
    传入 cuda 设备等）——这两类问题的共同点是：重新在 CPU 上加载模型
    并重试，通常能让任务继续跑完，而不是让用户面对一条难懂的 CUDA
    报错、手动去改设置重新提交。

    与 WhisperXAligner._transcribe_with_oom_retry() 只判断"out of
    memory"字样不同，这里额外覆盖几类同样应该触发"整体切到 CPU"的
    环境类报错关键词（CUDA driver/toolkit 缺失或不匹配、无法初始化
    CUDA context 等），因为 Qwen3-ForcedAligner / NeMo Forced Aligner
    没有 batch_size 可降，这些错误单纯重试 GPU 大概率还是失败，不如
    直接判定为"需要切 CPU"。

    只匹配运行时错误信息里的关键词，不依赖具体异常类型（不同版本的
    torch / transformers / nemo_toolkit 在这些场景下抛出的异常类型不
    完全一致，关键词匹配是更稳妥的兼容方式）。
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


def _safe_device(requested: str) -> str:
    """
    将 'auto' / 'cuda' 解析为真正可用的设备字符串。

    - 'auto'  → 先做 smoke-test，能用 CUDA 就 'cuda'，否则 'cpu'
    - 'cuda'  → smoke-test 失败则自动降级为 'cpu' 并打印警告
    - 'cpu'   → 直接返回
    """
    if requested == "cpu":
        return "cpu"
    if requested == "auto":
        return "cuda" if _torch_cuda_usable() else "cpu"
    if requested.startswith("cuda"):
        if _torch_cuda_usable():
            return requested
        logger.warning(
            f"[device] 请求 '{requested}' 但 PyTorch 未编译 CUDA 支持，"
            "自动回退到 CPU。若需 GPU 加速，请在 .mfa_env 中安装 CUDA 版 PyTorch："
            " pip install torch --index-url https://download.pytorch.org/whl/cu121"
        )
        return "cpu"
    return requested


# ═════════════════════════════════════════════════════════════════════════════
# 1. 模型文件路径管理（模块加载时立即执行，确保在任何 HF 导入之前完成）
# ═════════════════════════════════════════════════════════════════════════════

def resolve_models_dir() -> Path:
    """
    解析模型文件根目录。
    优先读取 TSUBAKI_MODELS_DIR 环境变量；否则使用 <backend>/models/。
    """
    env = os.environ.get("TSUBAKI_MODELS_DIR", "").strip()
    if env:
        p = Path(env).resolve()
        logger.info(f"[alt_aligners] 使用环境变量 TSUBAKI_MODELS_DIR: {p}")
    else:
        p = Path(__file__).resolve().parent / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── 目录常量 ──────────────────────────────────────────────────────────────────
_MODELS_DIR:    Path = resolve_models_dir()
_HF_CACHE:      Path = _MODELS_DIR / "hf_cache"   # HuggingFace Hub 缓存
_HF_HUB:        Path = _HF_CACHE   / "hub"        # transformers 子目录
_WHISPER_CACHE: Path = _MODELS_DIR / "whisper"    # OpenAI Whisper 模型缓存
_TORCH_CACHE: Path = _MODELS_DIR / "torch_cache"
_TORCH_CACHE.mkdir(parents=True, exist_ok=True)

for _d in (_HF_CACHE, _HF_HUB, _WHISPER_CACHE):
    _d.mkdir(parents=True, exist_ok=True)

# 将 HuggingFace 缓存重定向到 backend/models/hf_cache/
# 使用 setdefault 不覆盖用户已配置的环境变量
os.environ.setdefault("HF_HOME",                       str(_HF_CACHE))
os.environ.setdefault("HF_HUB_CACHE",                  str(_HF_HUB))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE",         str(_HF_HUB))
os.environ.setdefault("TRANSFORMERS_CACHE",            str(_HF_HUB))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")   # 消除 Windows 警告
os.environ["TORCH_HOME"] = str(_TORCH_CACHE)

# HF_HUB_OFFLINE / HF_ENDPOINT（镜像站）由设置页面统一管理，见 app_settings.py。
# 必须在本文件下方任何 transformers / whisperx 相关的懒加载 import 触发之前
# 完成设置——此处属于模块顶层，早于所有懒加载函数体，满足这个要求。
try:
    from app_settings import apply_env_from_settings as _apply_hf_env_settings
    _apply_hf_env_settings()
except Exception as _settings_err:
    logger.warning(f"⚠️  读取模型下载设置失败（{_settings_err}），回退到默认自动检测模型更新模式")
    os.environ["HF_HUB_OFFLINE"] = "0"

logger.info(
    f"[alt_aligners] 模型目录: {_MODELS_DIR}\n"
    f"  HF 缓存   → {_HF_HUB}\n"
    f"  Whisper缓存 → {_WHISPER_CACHE}\n"
    f"  (NeMo Forced Aligner 模型缓存由独立的 nemo_server.py 进程管理，"
    f"详见 backend/models/nemo_cache 与 nemo_hf_cache)"
)


# ═════════════════════════════════════════════════════════════════════════════
# 1b. PyTorch 2.6+ 兼容性补丁
#     PyTorch 2.6 起 torch.load 默认 weights_only=True，可能导致部分
#     HuggingFace 模型权重（内含自定义对象，如 omegaconf.ListConfig 等）
#     加载失败，抛出 _pickle.UnpicklingError。
#     这些权重来自官方 HF 仓库，可信，因此在模块加载时统一把
#     torch.load 的默认行为改回 weights_only=False。
# ═════════════════════════════════════════════════════════════════════════════
try:
    import torch as _torch

    if not getattr(_torch.load, "_tsubaki_patched", False):
        _original_torch_load = _torch.load

        def _patched_torch_load(*args, **kwargs):
            kwargs.setdefault("weights_only", False)
            return _original_torch_load(*args, **kwargs)

        _patched_torch_load._tsubaki_patched = True
        _torch.load = _patched_torch_load
        logger.info(
            "[alt_aligners] 已应用 torch.load 兼容性补丁 "
            "(weights_only 默认改为 False)"
        )
except ImportError:
    pass  # torch 未安装时跳过；Qwen3 后端本身也用不了


# ═════════════════════════════════════════════════════════════════════════════
# 1c. transformers "torch.load 版本门禁" 兼容性补丁
#     transformers 新版本加入了 check_torch_load_is_safe()：只要检测到当前
#     安装的 torch < 2.6，就直接拒绝加载任何非 safetensors 格式的权重文件
#     （CVE-2025-32434，torch.load 反序列化漏洞的官方修复），跟上面 1b 的
#     weights_only 参数完全无关——即使 1b 已经把 weights_only 改回了
#     False，这道检查依然会在 torch.load 真正被调用之前就直接抛出
#     ValueError 拦下。
#
#     WhisperX 默认使用的中文强制对齐模型
#     jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn（以及不少同类
#     老牌 wav2vec2 CTC 模型）只发布了旧式 pytorch_model.bin，没有
#     safetensors 版本，在 torch < 2.6 的环境下会被这道门禁拦下，报错
#     "could not be found in huggingface"——报错信息具有误导性，实际不是
#     模型不存在，而是加载器主动拒绝加载这个已经下载到本地的文件。
#
#     根治办法是把 venv 里的 torch 升级到 >= 2.6；但这可能牵动同一 venv
#     里 ctranslate2 / faster-whisper 等对 torch 版本有绑定要求的依赖，
#     不一定能立刻升级、且升级后需要重新验证 whisperx 各项功能仍然正常。
#     这里参照上面 1b 同样的思路，只针对这一道版本门禁本身打一个禁用
#     补丁：只信任 HuggingFace Hub 上下载量很大的公开官方模型（如这里
#     用到的 jonatasgrosman 系列，不涉及加载任何用户上传的不可信文件），
#     风险与 1b 跳过 weights_only 检查是同一类可接受的权衡。如果后续把
#     torch 升级到 >= 2.6，这个补丁自然变成等价的无操作（原始检查本身
#     也会通过），不需要手动回退或删除。
#
#     实现细节：transformers 内部是"from .utils.import_utils import
#     check_torch_load_is_safe"，把函数对象按值拷贝进了
#     transformers.modeling_utils 自己的命名空间——只替换
#     import_utils 模块上的属性，对"已经导入过 modeling_utils"的进程
#     不会生效（这也是为什么必须在任何 transformers 子模块被真正导入
#     之前，也就是本文件模块顶层这个位置打补丁）。为了同时覆盖"万一
#     transformers.modeling_utils 在此之前已经被别的地方导入过"这种
#     情况，下面额外尝试直接覆盖 modeling_utils 里的同名引用兜底。
# ═════════════════════════════════════════════════════════════════════════════
try:
    from transformers.utils import import_utils as _tf_import_utils

    if not getattr(_tf_import_utils.check_torch_load_is_safe, "_tsubaki_patched", False):
        def _patched_check_torch_load_is_safe(*_args, **_kwargs):
            return None

        _patched_check_torch_load_is_safe._tsubaki_patched = True
        _tf_import_utils.check_torch_load_is_safe = _patched_check_torch_load_is_safe

        # 兜底：如果 transformers.modeling_utils 已经被导入过并已经拷贝了
        # 旧的函数引用，这里直接覆盖它自己命名空间里的那一份。
        try:
            from transformers import modeling_utils as _tf_modeling_utils
            if hasattr(_tf_modeling_utils, "check_torch_load_is_safe"):
                _tf_modeling_utils.check_torch_load_is_safe = _patched_check_torch_load_is_safe
        except Exception:
            pass

        logger.info(
            "[alt_aligners] 已应用 transformers check_torch_load_is_safe "
            "兼容性补丁（仅放行 HuggingFace 官方公开模型的非 safetensors "
            "权重加载；根治办法是把 torch 升级到 >= 2.6）"
        )
except ImportError:
    pass  # transformers 未安装时跳过
except Exception as _tf_patch_err:
    logger.warning(
        f"[alt_aligners] transformers 兼容性补丁应用失败（不影响其他功能，"
        f"如遇 wav2vec2 对齐模型加载报错可手动升级 torch 至 >= 2.6）: "
        f"{_tf_patch_err}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# 2. 语言代码映射
# ═════════════════════════════════════════════════════════════════════════════

def _to_qwen_lang_name(lang: str) -> Optional[str]:
    """
    内部语言代码 → 官方 qwen-asr 包 Qwen3ASRModel.transcribe() /
    Qwen3ForcedAligner.align() 所要求的完整语言名（如 "Chinese"）。

    依据官方示例 (QwenLM/Qwen3-ASR examples/example_qwen3_asr_transformers.py,
    examples/example_qwen3_forced_aligner.py)：language 参数接受完整英文语言名
    （"Chinese" / "English" / "Japanese" / "Korean" / "Cantonese" ...），
    不是 ISO 短代码；返回 None 表示交给 Qwen3-ASR 自动语言检测（仅 ASR 支持，
    ForcedAligner 必须显式指定语言）。
    """
    return {
        "cmn": "Chinese", "zh": "Chinese", "zh-cn": "Chinese", "mandarin": "Chinese",
        "yue": "Cantonese", "cantonese": "Cantonese", "zh-yue": "Cantonese",
        "eng": "English", "en": "English", "english": "English",
        "jpn": "Japanese", "ja": "Japanese", "japanese": "Japanese",
        "kor": "Korean", "ko": "Korean", "korean": "Korean",
    }.get(lang.lower())


def _normalize_lang(lang: str) -> str:
    """各种语言代码 → 内部短代码 (zh / yue / en / ja / ko)"""
    return {
        "cmn": "zh", "zh-cn": "zh", "mandarin": "zh",
        "yue": "yue", "cantonese": "yue", "zh-yue": "yue",
        "eng": "en", "english": "en",
        "jpn": "ja", "japanese": "ja",
        "kor": "ko", "korean": "ko",
    }.get(lang.lower(), lang.lower())


def _to_whisperx_lang(lang: str) -> str:
    """内部语言代码 → WhisperX / Whisper 语言代码"""
    return {
        "cmn": "zh", "zh": "zh", "zh-cn": "zh",
        "yue": "zh",   # 粤语用 zh 近似；WhisperX 暂无独立粤语对齐模型
        "eng": "en", "en": "en",
        "jpn": "ja", "ja": "ja",
        "kor": "ko", "ko": "ko",
    }.get(lang.lower(), lang.lower())


# ═════════════════════════════════════════════════════════════════════════════
# 2b. WhisperX 对齐前文本预处理
#     WhisperX 强制对齐依赖"单调时间映射假设"：text ≈ audio 的顺序单调映射。
#     结构化文本（编号、列表符号、markdown 标题、CJK 标点）会破坏该假设，
#     导致 word-level alignment 失败甚至崩溃。
#     本函数在对齐前将参考文本口语化，使其对 wav2vec2 模型更友好。
# ═════════════════════════════════════════════════════════════════════════════

import re as _re
import re  # Bug修复: normalize_text_for_whisperx 函数体使用裸 re 名，需同时暴露非别名版本

def normalize_text_for_whisperx(text: str, lang: str = "zh") -> str:
    """
    轻量清洗版本（保留句子结构）
    专为 forced alignment / VOCALOID / SynthV 设计
    """

    if not text:
        return text

    # 1. 去 Markdown 标题
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)

    # 2. 去列表符号（保留内容）
    text = re.sub(r"(?m)^\s*(\d+[.、）)]|[①②③④⑤⑥⑦⑧⑨⑩]+|[一二三四五六七八九十]+[、.])\s*", "", text)

    # 3. ⚠️只替换“部分标点”，保留句末符号！
    text = re.sub(r"[「」『』【】《》〈〉]", " ", text)

    # 4. 英文引号/括号
    text = re.sub(r'[\"\'()\[\]{}]', " ", text)

    # 5. 空白统一
    text = re.sub(r"[ \t\r]+", " ", text)

    return text.strip()


# ═════════════════════════════════════════════════════════════════════════════
# 3. 工具函数
# ═════════════════════════════════════════════════════════════════════════════

class _MI:
    """模拟 textgrid.Interval，供 MFAProcessor 内部逻辑使用（时间单位：秒）"""
    __slots__ = ("minTime", "maxTime", "mark", "text")

    def __init__(self, start_sec: float, end_sec: float, mark: str):
        self.minTime = float(start_sec)
        self.maxTime = float(end_sec)
        self.mark = mark
        self.text = mark


def _is_cjk_punct(text: str) -> bool:
    """
    判断字符串是否全为标点 / 空白 / 符号（用于过滤 ASR 输出中的标点字符）。

    注：标点本身不可发音，不应出现在 LAB 的音素层。
    中文句末停顿（。！？）和停顿符（，、）对应的 LAB 条目由
    _fill_silences_lab() 根据时间轴间隙自动插入 SP / SIL。
    """
    if not text:
        return True
    for ch in text:
        cat = unicodedata.category(ch)
        if not cat.startswith(("P", "Z", "S")):
            return False
    return True


def _clean_align_text(text: str) -> str:
    """
    清洗送入强制对齐模型（wav2vec2 / whisperx.align）的文本：剥离标点
    符号，但保留空白（词边界）和单词内部撇号（英语缩略形式如
    "what's"）。

    【关键 bug 修复】此前直接复用 _is_cjk_punct() 逐字符过滤这段文本：
    该函数把空白字符（Unicode 类别 Z*，包括普通空格）也判定为"标点"
    一并清除，导致任何依赖空格分词的语言（英语、韩语等）在送进
    whisperx.align() 之前所有空格被吃掉、整句被拼接成一个不可分割
    的"伪单词"（如 "Hello world, What's Up" → "HelloworldWhatsUp"）。
    wav2vec2 按空白切词得到的 words 列表因此永远只有 1 个跨越全句的
    条目，下游既无法按英语单词切分做逐词 G2P，也无法在 ASR 词典/
    g2p_en 中查到这个被拼接出来的生造词，只能整句原样兜底输出。
    中文/日语本身走字符级（chars）通道、不依赖空格分词，这个 bug
    此前被掩盖，只在英语/韩语（走 words 通道）上暴露。

    撇号特殊保留：去掉撇号会让 "what's" 在发音词典里查不到对应词条
    （词典存的是 "what's" 而非 "whats"），被迫退化为整词输出。
    """
    if not text:
        return text
    out_chars: List[str] = []
    for ch in text:
        if ch.isspace():
            out_chars.append(" ")
            continue
        if ch in ("'", "\u2019"):   # ASCII 撇号 / 右单引号（缩略形式）
            out_chars.append(ch)
            continue
        if _is_cjk_punct(ch):
            continue
        out_chars.append(ch)
    return _re.sub(r"\s+", " ", "".join(out_chars)).strip()


def _fill_silences_lab(
    lab_content: str,
    min_gap_100ns: int = 500_000,       # 50ms
    long_sil_100ns: int = 5_000_000,    # 500ms → 统一输出 SIL
) -> str:
    """
    扫描 LAB 时间轴，在 ≥ 50ms 的间隙自动补全 SIL 条目。

    背景：Qwen3 不输出标点字符的时间戳，但句末/句中停顿
    会在相邻字符之间留下时间间隙，本函数将这些间隙转换为 LAB 静音标记。

    Parameters
    ----------
    min_gap_100ns : 插入静音的最小间隙（默认 50ms）
    long_sil_100ns : 超过此值仍输出 SIL（保留参数以兼容旧调用）
    """
    if not lab_content.strip():
        return lab_content

    parsed: List[Tuple[int, int, str]] = []
    for line in lab_content.strip().splitlines():
        parts = line.strip().split()
        if len(parts) >= 3:
            try:
                parsed.append((int(parts[0]), int(parts[1]), parts[2]))
            except ValueError:
                continue

    if not parsed:
        return lab_content

    result: List[Tuple[int, int, str]] = []

    # ── 音频开头静音 ──────────────────────────────────────────────────────
    if parsed[0][0] > min_gap_100ns:
        result.append((0, parsed[0][0], "SIL"))

    for i, (s, e, ph) in enumerate(parsed):
        result.append((s, e, ph))
        if i + 1 < len(parsed):
            gap_s = e
            gap_e = parsed[i + 1][0]
            gap   = gap_e - gap_s
            if gap > min_gap_100ns:
                result.append((gap_s, gap_e, "SIL"))

    return "\n".join(f"{s} {e} {p}" for s, e, p in result)


def _count_spoken_chars(text: str, int_lang: str) -> int:
    """统计参考文本中的可发音"单元"数（排除标点/空白），用于与 entries 数量对比。

    【历史 bug 说明 / 2026-07 修复】旧实现对 zh/yue/ja 只计落在 CJK 码位
    区间内的字符，混进参考文本里的拉丁字母（缩写逐字母朗读、型号名、
    "AI"/"WAV"/"RMVPE" 之类）会被直接无声丢弃、既不计数也不当标点跳过。
    但 aligner 对这些拉丁字母词是按"整串字母 = 1 条时间戳"的粒度产生
    真实对齐条目的（不是逐字母），于是旧实现统计出的参考字符数系统性
    偏低——偏低量等于文本里拉丁字母"词"的个数——导致下游告警
    "参考文本可发音字符数 ≠ 对齐条目数" 在任何混有英文的 CJK 稿子上
    都会稳定触发一次误报，且这个偏差量与音频是否分段、切在哪儿完全
    无关（纯粹是文本计数口径错误，不是对齐或分段合并的问题）。

    现在的处理：连续的拉丁字母串整体计为 1 个单元（与 aligner 的英文
    对齐粒度对齐，不能逐字母计数，否则同样会和真实 entries 数对不上）；
    CJK 字符仍按单字/单假名计数；标点/空白/符号一律不计。
    """
    if int_lang in ("zh", "yue", "ja", "ko"):
        count = 0
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch.isascii() and ch.isalpha():
                j = i
                while j < n and text[j].isascii() and text[j].isalpha():
                    j += 1
                count += 1          # 一整串拉丁字母 = 1 个词级对齐单元
                i = j
                continue
            cat = unicodedata.category(ch)
            if cat.startswith(("P", "Z", "S")):
                i += 1
                continue
            if int_lang in ("zh", "yue"):
                if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
                    count += 1
            elif int_lang == "ja":
                if '\u3040' <= ch <= '\u30ff' or '\u4e00' <= ch <= '\u9fff':
                    count += 1
            else:  # ko：原实现里缺失的分支（原来落到 else 靠 ch.strip() 兜底），这里显式补上
                if '\uac00' <= ch <= '\ud7a3':
                    count += 1
            i += 1
        return count
    else:
        count = 0
        for ch in text:
            cat = unicodedata.category(ch)
            if cat.startswith(("P", "Z", "S")):
                continue
            if ch.strip():
                count += 1
        return count


# ── 句末/句内标点 → 停顿时长映射 ──────────────────────────────────────────────
_HEAVY_END_PUNCT = "。！？…!?"
_LIGHT_END_PUNCT = "、，；,;"

# 内部静音占位符。必须为小写 "sil"，原因：
#   1. MFAProcessor.SIL_PHONES（mfa_processor.py）以小写匹配 "sil"/"sp"/"spn" 等，
#      _process_zh_words / _process_en_words / _process_yue_words 命中后一律
#      原样转换为 LAB 中的字面量 "sil"，且不消耗参考文本的拼音/音节配额。
#   2. phoneme_converter.merge_lab_silence() 把字面量 "sil" 当作"不可修改/
#      不可删除"的边界标记（用于正确处理相邻 "-" 辅音声母标记的吸收/丢弃逻辑），
#      而 "sp" 等其他变体不享有这条特殊保护——日语路径（_ja_entries_to_lab）
#      会用到这条规则，因此这里统一用 "sil" 而不是 "sp"。
_SIL_MARK = "sil"

# ── CTC 拉伸修复参数 ──────────────────────────────────────────────────────────
# CTC blank frame 无"强制停顿"概念：blank frame 在最优路径下被分配给短语末尾
# 的最后一个 token，导致换气/停顿处 token 时长被系统性拉长（实测 1.2s～1.7s）。
# 以下常量定义各类 token 的时长上限，超过 _CTC_MAX_*_SEC + _CTC_MIN_SP_SEC
# 的 token 会被截断，多出的时长转为 SP 静音标记。
#
# 典型问题案例（来自实际日志）：
#   话(1.36s)  啦(1.34s)  来(1.22s)  害(1.24s)  杖(1.14s)  。(1.74s)
# 均发生在短语内部的呼气/换气停顿处，而非仅限于句末。
#
# 【可调设置】下面这 4 个 _CTC_MAX_*_SEC / _CTC_MIN_SP_SEC 常量，以及本文件
# 后面的 QWEN3_FA_ONSET_DELAY_SEC / QWEN3_ASR_ONSET_DELAY_SEC /
# _QWEN3_FA_MIN_SYL_DUR_SEC / _QWEN3_ASR_MIN_SYL_DUR_SEC 共 8 个参数，现在
# 都可以在设置页面（SettingsPage.vue）里实时调整，经 app_settings.py 的
# get_alignment_tuning() 读取——本文件只在主进程内运行（不涉及 Qwen3 / NeMo
# 两个独立子服务），保存设置后下一次对齐任务立即生效，无需重启。
# 这里保留的字面量仅作为 app_settings.DEFAULT_SETTINGS 读取失败时的兜底值。
_CTC_PARTICLES: frozenset = frozenset('啦呀嘛哦呢吧了的地得着过噢喔哈嗯唔嘞哇')
_CTC_MAX_CJK_CHAR_SEC:    float = 0.50   # CJK 单字符最大时长（兜底默认值）
_CTC_MAX_CJK_PARTICLE_SEC: float = 0.35  # 语气词最大时长，通常更短（兜底默认值）
_CTC_MAX_EN_WORD_SEC:     float = 1.20   # 英文单词最大时长，宽松（兜底默认值）
_CTC_MIN_SP_SEC:          float = 0.15   # 低于此值的剩余时长不值得插入 SP（兜底默认值）


def _get_alignment_tuning() -> Dict[str, float]:
    """
    从 app_settings.py 实时读取一批可调对齐参数；读取失败（如 app_settings
    模块不可用、配置文件损坏）时安全回退到本文件顶部定义的字面量默认值，
    保证 alt_aligners.py 在任何情况下都能独立工作，不会因为设置模块异常
    而影响对齐主流程。
    """
    # 注意：QWEN3_FA_ONSET_DELAY_SEC 等 4 个常量定义在本文件更靠后的位置
    # （Qwen3-ForcedAligner 全局偏移校正一节），但本函数是在被调用时才执行
    # （而不是模块加载时），届时这些模块级常量早已就绪，用 globals().get()
    # 兼容的写法只是为了防御性地避免在极端重构场景下的 NameError。
    fallback = {
        "qwen3_fa_onset_delay_sec": globals().get("QWEN3_FA_ONSET_DELAY_SEC", 0.06),
        "qwen3_asr_onset_delay_sec": globals().get("QWEN3_ASR_ONSET_DELAY_SEC", 0.06),
        "qwen3_fa_min_syl_dur_sec": globals().get("_QWEN3_FA_MIN_SYL_DUR_SEC", 0.02),
        "qwen3_asr_min_syl_dur_sec": globals().get("_QWEN3_ASR_MIN_SYL_DUR_SEC", 0.02),
        "ctc_max_cjk_char_sec": _CTC_MAX_CJK_CHAR_SEC,
        "ctc_max_cjk_particle_sec": _CTC_MAX_CJK_PARTICLE_SEC,
        "ctc_max_en_word_sec": _CTC_MAX_EN_WORD_SEC,
        "ctc_min_sp_sec": _CTC_MIN_SP_SEC,
        # 【v3】qwen3_fa_chunk_threshold_sec / qwen3_fa_chunk_target_sec 这两个
        # 纯音频时长维度的分段阈值/目标长度已废弃并从这里移除——分段与否、
        # 切在哪，现在完全由参考文本自身的句末标点决定（见 _align_chunked /
        # _plan_sentence_aligned_chunks 顶部说明），不再需要这两个参数。
        # 按句分段模式下，单句时长的下限/上限（详见 _plan_sentence_aligned_
        # chunks() 顶部说明）；需要出现在这里才能被下面 `if k in fallback`
        # 的过滤条件放行，不然设置页面里改了也不会生效。
        "qwen3_fa_min_sentence_chunk_sec": 3.0,
        "qwen3_fa_max_sentence_chunk_sec": 20.0,
    }
    try:
        import app_settings
        tuning = app_settings.get_alignment_tuning()
        fallback.update({k: v for k, v in tuning.items() if k in fallback})
    except Exception as e:
        logger.debug("读取对齐调优设置失败，使用内置默认值: %s", e)
    return fallback


def _get_whisperx_prepass_settings() -> Dict[str, object]:
    """
    读取"Qwen3-FA 长音频分段前，先用 WhisperX 做一次粗测时间戳"这一开关
    及其使用的 Whisper 模型档位（详见 _plan_chunks_via_whisperx_rough_pass
    顶部说明）。

    与 _get_alignment_tuning() 不同：那边全部是数值型调优参数，走
    app_settings.get_alignment_tuning() 专用的 float 通道；这里一个是
    bool 开关、一个是字符串，直接读 app_settings.load_settings() 的
    原始字典即可，没有必要为此单独扩展 get_alignment_tuning() 的类型
    约定。本函数只在每次 Qwen3-FA align() 任务开始时调用一次（不是
    每个分段调用一次），无需像 _get_alignment_tuning() 那样做 mtime
    缓存，直接读盘即可。

    读取失败（app_settings 不可用、配置文件损坏）时安全回退到"关闭"，
    与该功能默认不开启保持一致，不影响任何现有行为。
    """
    fallback: Dict[str, object] = {
        "enabled": False,
        "whisper_model": "large-v3",
    }
    try:
        import app_settings
        settings = app_settings.load_settings()
        fallback["enabled"] = bool(settings.get("qwen3_fa_use_whisperx_prepass", False))
        model = str(settings.get("qwen3_fa_whisperx_prepass_model") or "").strip()
        if model:
            fallback["whisper_model"] = model
    except Exception as e:
        logger.debug("读取 WhisperX 粗测设置失败，使用默认值（关闭）: %s", e)
    return fallback


def _get_sentence_chunking_enabled() -> bool:
    """
    读取"Qwen3-ForcedAligner 按句子分段对齐"总开关
    （qwen3_fa_enable_sentence_chunking，默认 False/禁用）。

    该开关是 _align_chunked() 这一整套"按句末标点规划分段 + 逐段独立
    对齐"流程的总闸门：为 False 时，Qwen3ForcedAligner.align() 会完全
    跳过 _align_chunked()，直接调用 _align_single_chunk() 做整段单次
    对齐，行为等同于 v3 分段逻辑引入之前的原始版本；WhisperX 粗测预处理
    （_get_whisperx_prepass_settings）是分段流程内部的一个子选项，只在
    本开关为 True 时才可能被实际用到——两者的父子关系由
    app_settings.save_settings() 在保存时强制维护（总开关关闭时粗测
    预处理会被一并强制置为 False），这里读取到的值已经是校验过的结果，
    不需要再额外处理"父开关关闭但子开关仍为 True"这种矛盾状态。

    与 _get_whisperx_prepass_settings() 一样，只在每次 align() 任务
    开始时读取一次，不做 mtime 缓存；读取失败时安全回退到"关闭"，
    与该功能默认不开启保持一致，不影响任何现有行为。
    """
    try:
        import app_settings
        settings = app_settings.load_settings()
        return bool(settings.get("qwen3_fa_enable_sentence_chunking", False))
    except Exception as e:
        logger.debug("读取按句子分段对齐总开关失败，使用默认值（关闭）: %s", e)
        return False


def _get_whisperx_batch_size() -> int:
    """
    读取 WhisperX ASR 转录使用的 batch_size 调优设置（默认 16，与
    WhisperXAligner.__init__ 原有默认值一致）。

    单独抽出来实时读取（而不是依赖 WhisperXAligner 实例构造时保存的
    self.batch_size），是因为该实例作为跨任务复用的单例缓存（见
    get_aligner()），用户在设置页面调完这个值之后，已经创建好的实例
    不会重新构造——只有每次转录都重新读一次设置，修改才能在下一次
    任务上立即生效，不需要重启进程，与其余对齐调优参数的约定一致。
    详见 WhisperXAligner._transcribe_with_oom_retry() 顶部说明。

    读取失败或配置值非法时安全回退到 16。
    """
    try:
        import app_settings
        settings = app_settings.load_settings()
        val = int(settings.get("whisperx_batch_size", 16))
        return max(1, val)
    except Exception:
        return 16


def _get_qwen3_batch_size() -> int:
    """
    读取 Qwen3-ASR / Qwen3-ForcedAligner / NeMo Forced Aligner 共用的
    batch_size 调优设置（默认 8）。

    与 _get_whisperx_batch_size() 同样的"实时读取、无需重启"约定，见
    app_settings.get_qwen3_batch_size() 顶部说明。三个后端的具体用法：
      - Qwen3-ASR：通过 HTTP 请求体的 "batch_size" 字段透传给
        qwen3_server.py，服务端据此设置 max_inference_batch_size。
      - Qwen3-ForcedAligner：作为 _align_single_chunk() 显存不足时自动
        降级重试的起始批大小参考值（见该方法内 OOM 重试逻辑）。
      - NeMo Forced Aligner：通过 HTTP 请求体的 "batch_size" 字段透传给
        nemo_server.py，服务端据此决定 OOM 重试的起始降级参考值。

    读取失败或配置值非法时安全回退到 8。
    """
    try:
        import app_settings
        return app_settings.get_qwen3_batch_size()
    except Exception:
        return 8

# ─────────────────────────────────────────────────────────────────────────
# SudachiPy 惰性单例；
# 词典加载较慢，进程内只创建一次。
# ─────────────────────────────────────────────────────────────────────────
_sudachi_tokenizer_obj = None
_sudachi_split_mode_obj = None


def _get_sudachi_tokenizer():
    """返回 (tokenizer_obj, split_mode) 单例，首次调用时加载词典。"""
    global _sudachi_tokenizer_obj, _sudachi_split_mode_obj
    if _sudachi_tokenizer_obj is None:
        from sudachipy import dictionary, tokenizer as sudachi_tokenizer
        _sudachi_tokenizer_obj = dictionary.Dictionary().create()
        # SplitMode.C：最长单位切分（更接近自然分词，多音字读音更准确）
        _sudachi_split_mode_obj = sudachi_tokenizer.Tokenizer.SplitMode.C
    return _sudachi_tokenizer_obj, _sudachi_split_mode_obj


def _kata_to_hira(text: str) -> str:
    """片假名 → 平假名（纯 Unicode 码位偏移，无第三方依赖）。

    片假名 U+30A1-U+30F6 与平假名 U+3041-U+3096 之间存在固定偏移 0x60；
    非片假名字符（如长音符 'ー'、促音、汉字、标点）原样保留。
    """
    out = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return "".join(out)


# 拗音 / 外来语小假名：与前一个假名合并为同一个 mora（如 'きゃ' 算 1 个
# mora，而不是 2 个）。促音「っ」和拨音「ん」不在此列，它们各自单独成 mora。
_JA_SMALL_KANA = frozenset("ゃゅょぁぃぅぇぉ")


def _split_ja_mora(text: str) -> List[str]:
    """
    把 Sudachi reading_form() 给出的假名读音字符串拆分为 mora 列表。

    用途：_ja_entries_to_lab() 中，多字符 morpheme（如「気持ち」→ きもち）
    原本会把整段读音揉成一条跨越全词时间区间的 LAB 行，丢弃了 wav2vec2
    本来给出的逐字符时间戳。当 morpheme 的表层字数与读音 mora 数恰好相等
    时（纯假名词、大多数简单汉字词都满足），可以把读音逐字拆回各自原始
    时间戳，恢复逐字精度；字数与 mora 数不一致时（典型如「大変」2 字
    对应 たいへん 4 mora、「僕」1 字对应 ぼく 2 mora），由
    _distribute_mora_across_chars() 把 mora 尽量均匀地分配给各个原始
    字符、再在每个字符自身时间戳内部按 mora 数等分，不再退回整词合并。
    """
    morae: List[str] = []
    for ch in text:
        if ch in _JA_SMALL_KANA and morae:
            morae[-1] += ch
        else:
            morae.append(ch)
    return morae


def _distribute_mora_across_chars(
    piece: List[Tuple[float, float, str]],
    mora_list: List[str],
) -> List[str]:
    """
    把一个 morpheme 的 mora 序列分配给该 morpheme 对应的原始字符时间戳
    （piece），用于字符数与 mora 数不一致的情况（如「僕」1 字对应
    ぼく 2 mora、「大変」2 字对应 たいへん 4 mora）。

    【这是本次修复的核心】此前遇到这种不一致就直接放弃逐字精度，把
    整段读音揉成一条跨越整个 morpheme 时间区间的 LAB 行（见
    _split_ja_mora 旧版文档字符串），这正是用户反馈"音标连在一起，
    不是一个假名一个音标"的根因——「僕」「本」「大変」等词被输出成
    单条 LAB 行，内部塞进了 2~4 个假名。

    分配规则：
      1. mora 总数（通常 ≥ 字符数，因为一个汉字常对应 1~2 个 mora）
         按字符顺序尽量平均分组（divmod 取整，余数分给靠前的字符），
         每个字符至少分到 1 个 mora。
      2. 罕见的反向情况（字符数多于 mora 数）：分不到 mora 的字符并入
         前一个字符的分组，与其共享同一组 mora 的时间戳，避免产生
         空区间。
      3. 每个字符（或合并后的字符组）分到的 mora 子序列，在该字符
         自身已有的 [start, end] 时间戳内部按数量等分——只细分已知
         的字符级时间戳，不触碰字符之间的边界，不会引入新的跨字符
         错位。这是在"没有逐 mora 强制对齐证据"前提下合理的最佳近似，
         且明显优于把整个词压成一条目。
    """
    n_chars = len(piece)
    n_mora = len(mora_list)
    if n_chars == 0 or n_mora == 0:
        return []

    if n_mora >= n_chars:
        base, rem = divmod(n_mora, n_chars)
        counts = [base + (1 if i < rem else 0) for i in range(n_chars)]
    else:
        # 字符数多于 mora 数：前 n_mora 个字符各分 1 个，其余分 0，
        # 稍后并入前一组。
        counts = [1] * n_mora + [0] * (n_chars - n_mora)

    merged_piece: List[Tuple[float, float]] = []
    merged_counts: List[int] = []
    for (ps, pe, _ch), c in zip(piece, counts):
        if c == 0 and merged_piece:
            prev_s, _prev_e = merged_piece[-1]
            merged_piece[-1] = (prev_s, pe)
        else:
            merged_piece.append((ps, pe))
            merged_counts.append(c)
    if merged_counts and merged_counts[0] == 0:
        # 防御性兜底：理论上不会出现（n_mora>=1 时第一个字符必分到
        # 至少 1 个 mora），仅避免万一发生时丢失这组 mora。
        merged_counts[0] = 1

    lines: List[str] = []
    mora_idx = 0
    for (ps, pe), c in zip(merged_piece, merged_counts):
        group = mora_list[mora_idx: mora_idx + c]
        mora_idx += c
        if not group:
            continue
        if len(group) == 1:
            lines.append(f"{int(ps*10_000_000)} {int(pe*10_000_000)} {group[0]}")
            continue
        dur = pe - ps
        step = dur / len(group)
        for j, mora in enumerate(group):
            sub_s = ps + step * j
            sub_e = pe if j == len(group) - 1 else ps + step * (j + 1)
            lines.append(f"{int(sub_s*10_000_000)} {int(sub_e*10_000_000)} {mora}")

    return lines


def _explode_to_single_char_entries(
    entries: List[Tuple[float, float, str]],
) -> List[Tuple[float, float, str]]:
    """
    把 (start, end, text) 条目里 text 长度 > 1 的项，按字符数拆成多条
    单字符条目，时间在原 [start, end] 区间内等分。text 长度已经是 1
    的条目原样返回（不做任何浮点重算，避免不必要的精度损耗）。

    【Bug 背景】_ja_entries_to_lab() 用 Sudachi 给 joined_text 分词后，
    按"当前 morpheme 表层字符数 n = len(surface)"去切 char_entries
    列表：`piece = char_entries[idx: idx+n]`，`idx += n`。这隐含一个
    前提：char_entries 列表里每一项正好对应 1 个原始文字字符——这样
    "表层字符数"和"要跨过的列表下标数"才是同一个数。

    WhisperX 的 chars 输出（return_char_alignments=True）天然满足这个
    前提：本来就是逐字符给时间戳，1 entry = 1 character。但
    Qwen3-ForcedAligner 走的是它自己的（子词/BPE 类）tokenizer，常见
    汉字词（如"日本語"）、常见英文单词（如"English"）经常被它的词表
    当作一个完整单元一次性返回成 1 条 entry，text 却是 3 个或 7 个
    字符——这时 1 entry ≠ 1 character。Sudachi 那边算出的 n 仍然是
    "字符数"，但拿去切的是"列表下标"，两者一旦不再一一对应就会错位：
    多字符的那一条目被提前/重复消费，或者 idx 越界导致后面整段切出
    空 piece 被跳过——表现为这些汉字/英文在最终 LAB 里直接消失，正是
    本次要修复的现象。

    修复：在交给 Sudachi 分词之前，统一先把所有 entries 拆成"1 字符 =
    1 entry"。这样无论上游对齐器给出的是真正的字符级时间戳（WhisperX）
    还是词/子词级 token（Qwen3-FA），_ja_entries_to_lab() 后续看到的
    永远是字符级粒度，重新满足原本就只为这种粒度设计的下标消费逻辑。

    时间分配用简单的等分（按字符数平分原 entry 的时长），因为 Qwen3-FA
    只给了"这个词整体"的起止时间，没有给词内部逐字符的真实边界，等分
    是在没有更细粒度证据时唯一合理的近似——这与 phoneme_converter 里
    其它"整词时长按权重/数量分配给内部音素"的兜底逻辑（如
    distribute_arpabet_phones）是同一思路。
    """
    out: List[Tuple[float, float, str]] = []
    for s, e, t in entries:
        n = len(t)
        if n <= 1:
            out.append((s, e, t))
            continue
        dur = (e - s) / n
        for i, ch in enumerate(t):
            cs = s + i * dur
            ce = e if i == n - 1 else s + (i + 1) * dur
            out.append((cs, ce, ch))
    return out


def _bind_ref_text_by_asr_count(
    cleaned_ref: str,
    raw_segments: List[Dict],
    int_lang: str,
) -> bool:
    """
    按"每段 ASR 自己识别出的可发音字符数"作为配额，把完整参考文本
    （保留原始标点）按顺序切给对应的段，而不是直接放弃参考文本退回
    ASR 识别结果。

    关键点（区别于按时长比例分配的早期方案）：
    分配用的配额是每段 ASR 文本自身的字符数，而不是音频时长。
    这保证替换后 seg_text 的可发音字符数与该段稍后实际送入
    wav2vec2 做强制对齐的字符数完全一致——避免"参考文本字数与该段
    音频实际内容字数不匹配 → 强制对齐被迫拉伸/压缩 → 时间戳错位"
    的问题。按时长比例分配做不到这一点：时长占比只是粗略估算，
    跟该段 ASR 真实识别出的字符数可能完全不是一回事。

    全局总字数（ASR 总识别字数 vs 参考文本总字数）几乎总会有一定差异
    ——音频越长，ASR 漏听/多听的绝对字数也越容易变多，这是正常现象，
    不代表参考文本就跟音频对不上。早期版本一旦差异超过阈值就整体放弃
    替换，导致长文本几乎必然退回 ASR 的（几乎不带标点的）识别文本，
    这正是"长文本反而又粘连"的根因。

    现在改为：按比例把每段配额整体缩放，使配额总和精确等于参考文本
    总字数（用最大余数法取整），把全局差异均摊到每一段上，而不是
    集中甩给某一段或整体放弃——每段拿到的字符数仍然是一个跟它自身
    原始 ASR 字数接近的整数，强制对齐不会被某一段突然暴增/清零的
    字数带偏。

    会原地修改 raw_segments 中每个元素的 "text" 字段。
    返回 True 表示已替换；False 仅在彻底无法估算配额时返回（如总字数为 0）。
    """
    def _is_spoken(ch: str) -> bool:
        if int_lang in ("zh", "yue"):
            return '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf'
        if int_lang == "ja":
            return '\u3040' <= ch <= '\u30ff' or '\u4e00' <= ch <= '\u9fff'
        return ch.strip() != "" and not _is_cjk_punct(ch)

    # 每段 ASR 自己的可发音字符数 = 该段稍后会送入 wav2vec2 对齐的字符数
    quotas = [
        sum(1 for ch in (seg.get("text", "") or "") if _is_spoken(ch))
        for seg in raw_segments
    ]
    total_quota = sum(quotas)
    ref_spoken_total = sum(1 for ch in cleaned_ref if _is_spoken(ch))

    if total_quota == 0 or ref_spoken_total == 0:
        return False

    diff_ratio = abs(total_quota - ref_spoken_total) / max(ref_spoken_total, 1)
    logger.info(
        f"[alt_aligners] ASR 总识别字数={total_quota}，参考文本总字数="
        f"{ref_spoken_total}，差异 {diff_ratio:.1%}（按比例缩放各段配额，"
        "均摊差异，不整体放弃参考文本）"
    )

    # 按比例缩放配额，使其总和精确等于参考文本总字数（最大余数法取整）
    scale = ref_spoken_total / total_quota
    scaled = [q * scale for q in quotas]
    int_quotas = [int(x) for x in scaled]
    remainder = ref_spoken_total - sum(int_quotas)
    if remainder > 0:
        order = sorted(
            range(len(scaled)), key=lambda i: scaled[i] - int_quotas[i], reverse=True
        )
        for i in order[:remainder]:
            int_quotas[i] += 1

    cum_quota: List[int] = []
    acc = 0
    for q in int_quotas:
        acc += q
        cum_quota.append(acc)

    chunks = ["" for _ in raw_segments]
    spoken_seen = 0
    seg_idx = 0
    for ch in cleaned_ref:
        if _is_spoken(ch):
            spoken_seen += 1
            while seg_idx < len(cum_quota) - 1 and spoken_seen > cum_quota[seg_idx]:
                seg_idx += 1
        chunks[seg_idx] += ch

    for seg, chunk in zip(raw_segments, chunks):
        seg["text"] = chunk.strip()
    return True


_LATIN_LETTER_RE = re.compile(r"^[a-zA-Z']$")


def _merge_latin_letter_chars(
    seg_entries: List[Tuple[float, float, str]],
) -> List[Tuple[float, float, str]]:
    """
    把 WhisperX 字符级对齐（return_char_alignments=True）拆出的连续单个
    拉丁字母重新拼回完整英文单词。

    【Bug 修复背景】中/粤/日/韩都走 a_seg["chars"]（逐字符）取时间戳，
    这是 CJK 本身没有空格分词、必须逐字给时间戳的正确做法。但当参考
    文本里混有英文单词（如中文歌词里夹的 "Singing"）时，WhisperX 的
    char-level 输出会把这个英文单词也拆成一个个孤立字母："S" "i" "n"
    "g" "i" "n" "g"，每个字母各自占一条 (start, end, text) entry。

    这些孤立字母随后进入 _process_zh_words() / _process_yue_words()，
    每一条都会单独命中 _is_english_word()（该函数只检查字符集合是否
    全为字母，不关心长度），于是逐个调用 word_to_arpabet("s")、
    word_to_arpabet("g") ……而英文词典里单个字母本身就是合法词条（字母
    的拼读名，如 S→"ess"→eh s，G→"gee"→jh iy），得到的是"字母朗读音"
    而不是 "Singing" 这个词本身的发音——表现为整段英文被读成挨个拼读
    字母。

    修复思路：在送入 _process_*_words() 之前，把时间上连续、且每个
    text 都是单个拉丁字母（含内部撇号，兼容如 "o" "'" "n" 这种由
    don't 拆出的写法）的 entries 合并成一条，text 拼接还原成完整单词，
    时间跨度取首尾。一旦遇到非单字母 entry（CJK 字符、数字、标点、或
    已经是多字符的 token），当前合并立即结束，不会跨过真正的词/字边界。

    合并之后的整词 entry 会在 _process_zh_words() 里正常命中
    _is_english_word() → word_to_arpabet("singing")，按真实单词走
    G2P，得到该单词本身的音素序列，而不是逐字母拼读。

    仅在 zh/yue/ko 的 chars 分支调用（具体原因见调用处注释：ja 因
    Sudachi 的 surface-长度索引消费机制，不能做这个合并）；英语本身
    走 words 分支，不受影响，也不需要这个合并。
    """
    if not seg_entries:
        return seg_entries

    merged: List[Tuple[float, float, str]] = []
    run: List[Tuple[float, float, str]] = []

    def _flush_run():
        if not run:
            return
        if len(run) == 1:
            merged.append(run[0])
        else:
            s = run[0][0]
            e = run[-1][1]
            word = "".join(t for _, _, t in run)
            merged.append((s, e, word))
        run.clear()

    for entry in seg_entries:
        _, _, t = entry
        if _LATIN_LETTER_RE.match(t):
            run.append(entry)
        else:
            _flush_run()
            merged.append(entry)

    _flush_run()
    return merged


def _inject_sentence_pauses(
    seg_entries: List[Tuple[float, float, str]],
    seg_text: str,
    heavy_gap_sec: float = 0.08,
    light_gap_sec: float = 0.04,
    sil_mark: str = _SIL_MARK,
) -> List[Tuple[float, float, str]]:
    """
    在 seg_text 中句末/句内标点出现的位置，插入真正的静音条目
    （mark=sil_mark），而不只是挪动相邻字符的时间戳。

    【修复说明】旧版本只挪动了前一个字符的结束时间，在数值上腾出了
    一段空隙，但没有写入任何 SIL/SP 标记。下游 _word_entries_to_lab()
    会原样把这段"空隙"写进 LAB——但 LAB 里相邻两行时间戳之间的数值
    间隔，并不会被 SVP 工程生成阶段（tsubaki_processor._is_true_silence()
    / 步骤①"如果是 lab 里的显式静音标签 sil/pau/sp，直接跳过不生成音符"）
    识别为停顿，因为那一步只检查"这一行的 label 是不是静音词"，根本
    不会去看前后两行时间戳之间是否存在数值间隙。所以旧版本制造出的
    停顿在 LAB 里确实存在（时间戳对得上），但在 SVP 里完全不可见，
    音符还是会首尾相连——这正是"gan、ge、shu、ling 等句末音标连在
    一起"的根因。

    现在改为：
      1. 若该字符与下一个字符之间天然就有 ≥ target_gap 的时间间隙
         （wav2vec2 偶尔会留下这种间隙），直接把整个天然间隙转换成
         一条 sil 条目。
      2. 若天然间隙不够，从当前字符尾部"偷"出最多 target_gap 的时长
         （且最多偷取该字符自身时长的一半，避免压出负数/异常短音），
         在腾出来的空隙里插入 sil 条目。
      3. 不改变原有字符的相对顺序、不跨字符插队，因此不会引入新的
         拼音/音节错位；插入的 sil 条目会被 MFAProcessor 的
         _process_*_words() 识别（mark in SIL_PHONES）并原样转换为
         LAB 中的字面量 "sil"，不消耗参考文本的拼音配额（syl_index
         不会因为这条 sil 而前进）。

    heavy_gap_sec : 句末强标点（。！？…）对应的停顿时长
    light_gap_sec : 句内轻标点（、，；）对应的停顿时长
    sil_mark      : 插入的静音占位符文本（须为小写，与 SIL_PHONES 命名一致）

    注：对英文等以单词为单位对齐的语言，本函数仍按"参考文本里的单个
    字符"逐一计算停顿位置，与 entries（单词级）并非严格一一对应——
    这是沿用自旧版本的已知局限，不在本次修复范围内（本次修复的对象
    是中／日逐字对齐路径，这里维持原有行为不做改动）。
    """
    if not seg_entries or not seg_text:
        return seg_entries

    gap_after: List[float] = []
    n = len(seg_text)
    i = 0
    while i < n:
        ch = seg_text[i]
        if _is_cjk_punct(ch) or ch.isspace():
            i += 1
            continue
        j = i + 1
        while j < n and seg_text[j].isspace():
            j += 1
        gap = 0.0
        if j < n:
            if seg_text[j] in _HEAVY_END_PUNCT:
                gap = heavy_gap_sec
            elif seg_text[j] in _LIGHT_END_PUNCT:
                gap = light_gap_sec
        gap_after.append(gap)
        i += 1

    m = min(len(seg_entries), len(gap_after))
    result: List[Tuple[float, float, str]] = []
    for k in range(len(seg_entries)):
        s, e, t = seg_entries[k]

        if k < m - 1:
            target_gap = gap_after[k]
            if target_gap > 0:
                next_s = seg_entries[k + 1][0]
                avail = next_s - e
                if avail < target_gap:
                    shrink = min(target_gap - avail, max(0.0, (e - s) * 0.5))
                    if shrink > 0:
                        e = e - shrink
                result.append((s, e, t))
                # 腾出的空隙（天然间隙 + 借用时长）写成一条真正的 sil 条目，
                # 而不是仅仅留下一个数值上的空隙。
                if next_s > e:
                    result.append((e, next_s, sil_mark))
                continue

        result.append((s, e, t))

    return result


def _refine_sil_boundaries_by_energy(
    seg_entries: List[Tuple[float, float, str]],
    cropped_audio,
    sr: int,
    seg_start_sec: float,
    sil_mark: str = _SIL_MARK,
    frame_sec: float = 0.01,
    hop_sec: float = 0.005,
    rel_threshold: float = 0.06,
    abs_floor: float = 0.0008,
    abs_ceiling: float = 0.003,
    max_extra_claim_sec: float = 0.4,
    min_keep_sec: float = 0.06,
) -> List[Tuple[float, float, str]]:
    """
    用该句真实音频的短时能量，把 _inject_sentence_pauses() 插入的固定
    时长（40ms/80ms）sil 条目向左右两侧扩展到真正安静的区域边界。

    【背景】_inject_sentence_pauses() 给出的 40/80ms 只是"标点处至少要
    有多长停顿"的下限，不代表音频里真实停顿就是这么短。wav2vec2 字符级
    强制对齐没有静音词表，经常会把真正的换气/停顿时间错误地"焊"进
    紧邻标点前最后一个字的时长里——例如"令"字符的对齐区间可能一路
    延伸到下一句开始前，把演唱者实际换气的两三百毫秒真静音都算成
    "令"的发音时长，而 _inject_sentence_pauses() 只能在这段被吃掉的
    时长里再"借"出 40/80ms，借不出真正的停顿长度。

    【做法】对该句裁剪出的真实音频（cropped_audio）做短时 RMS 能量
    扫描，对每条 sil 条目：
      - 从 sil 起点向左探测：能量持续低于阈值就持续把 sil 起点往左推
        （即从前一个字符"偷"时长），直到能量回升到阈值以上（说明已
        经进入前一个字符真正的发声区）、或达到 max_extra_claim_sec
        上限、或前一个字符被压到 min_keep_sec 下限为止。
      - 从 sil 终点向右探测同理，找到下一个字符真正的发声起点。
      - 若标点处其实是连唱、从一开始能量就已经偏高（没有真实停顿），
        探测会在第一步就停下，不做任何扩展——保留
        _inject_sentence_pauses() 给出的最小停顿即可，不会把正常的
        延音误判为停顿来源。

    阈值 = clip(rel_threshold × 该句 70 分位能量, abs_floor, abs_ceiling)，
    既能随录音整体响度自适应，又不会因为某一句特别响/特别轻而跑到
    不合理的区间——这三个常量都是用本项目实际样例反复试出来的安全区间，
    不是理论推导值，如果某些素材效果不理想，优先调整 rel_threshold。

    注：本函数只移动已存在的 sil / 相邻字符边界，不会增删条目，因此
    不会打乱 _process_*_words() 的拼音/音节配额对应关系。
    """
    if not seg_entries or cropped_audio is None:
        return seg_entries
    try:
        import numpy as _np
    except ImportError:
        return seg_entries

    n_samples = len(cropped_audio)
    if n_samples == 0:
        return seg_entries

    frame_n = max(1, int(frame_sec * sr))
    hop_n = max(1, int(hop_sec * sr))
    n_frames = max(0, (n_samples - frame_n) // hop_n + 1)
    if n_frames < 2:
        return seg_entries

    audio64 = _np.asarray(cropped_audio, dtype=_np.float64)
    rms = _np.empty(n_frames, dtype=_np.float64)
    for fi in range(n_frames):
        st = fi * hop_n
        chunk = audio64[st: st + frame_n]
        rms[fi] = float(_np.sqrt(_np.mean(chunk * chunk))) if len(chunk) else 0.0

    voiced_level = float(_np.percentile(rms, 70))
    threshold = max(abs_floor, min(rel_threshold * voiced_level, abs_ceiling))

    def _rms_at(local_t: float) -> float:
        idx = int(round(local_t * sr / hop_n))
        idx = max(0, min(n_frames - 1, idx))
        return rms[idx]

    seg_len_sec = n_samples / float(sr)
    result = list(seg_entries)
    n = len(result)
    for k in range(1, n - 1):
        s, e, t = result[k]
        if t != sil_mark:
            continue
        prev_s, prev_e, prev_t = result[k - 1]
        next_s, next_e, next_t = result[k + 1]
        if prev_t == sil_mark or next_t == sil_mark:
            continue   # 理论上不会相邻出现两条 sil，安全起见跳过

        sil_s_local = s - seg_start_sec
        sil_e_local = e - seg_start_sec

        left_limit = max(
            0.0,
            (prev_s - seg_start_sec) + min_keep_sec,
            sil_s_local - max_extra_claim_sec,
        )
        probe = sil_s_local
        while probe - hop_sec >= left_limit and _rms_at(probe - hop_sec) < threshold:
            probe -= hop_sec
        new_sil_s_local = probe

        right_limit = min(
            seg_len_sec,
            (next_e - seg_start_sec) - min_keep_sec,
            sil_e_local + max_extra_claim_sec,
        )
        probe = sil_e_local
        while probe + hop_sec <= right_limit and _rms_at(probe + hop_sec) < threshold:
            probe += hop_sec
        new_sil_e_local = probe

        if new_sil_e_local <= new_sil_s_local:
            continue   # 探测结果异常（区间反转）则保持原状，不做改动

        new_sil_s = new_sil_s_local + seg_start_sec
        new_sil_e = new_sil_e_local + seg_start_sec

        result[k - 1] = (prev_s, new_sil_s, prev_t)
        result[k]     = (new_sil_s, new_sil_e, sil_mark)
        result[k + 1] = (new_sil_e, next_e, next_t)

    return result


# ═════════════════════════════════════════════════════════════════════════════
# 3b. CTC 短语边界拉伸修复
#
#     根本原因：WhisperX / wav2vec2 的 CTC forced alignment 无"强制停顿"概念，
#     只有 token 和 blank（空白帧）。CTC 最优路径会把短语边界处的所有 blank
#     frame 分配给最后一个 token，导致换气/停顿处 token 时长被系统性拉长。
#
#     现象：说话/演唱时短语末尾字符（如"话""啦""来""害"等）出现 1.2s～1.7s
#     超长时长，下游 LAB→SVP 流程将其转化为超长音符，音符首尾相连（legato）。
#
#     这与标点/句末无关——任何呼气停顿处（包括句子内部）都会出现。
#     _inject_sentence_pauses() 仅处理参考文本中有标点标记的位置（且上限为
#     40/80ms），无法解决这种无标点标记的短语内部呼气停顿。
# ═════════════════════════════════════════════════════════════════════════════

def _fix_ctc_stretch(
    entries: List[Tuple[float, float, str]],
    int_lang: str,
    max_cjk_char_sec:    Optional[float] = None,
    max_cjk_particle_sec: Optional[float] = None,
    max_en_word_sec:     Optional[float] = None,
    min_sp_sec:          Optional[float] = None,
) -> List[Tuple[float, float, str]]:
    """
    修复 WhisperX CTC 短语边界拉伸问题。

    对超过时长上限的 token 进行截断，并将多出的时长转为 SP 静音条目，
    供 _refine_sil_boundaries_by_energy() 进一步精化至真实静音边界。

    **调用时机**：在 _inject_sentence_pauses() 之后、
    _refine_sil_boundaries_by_energy() 之前。

    之所以在 _inject_sentence_pauses 之后调用：
      _inject_sentence_pauses 按"seg_text 中第 k 个可发音字符对应
      seg_entries[k]"的逐索引映射工作。若在它之前就插入 SP，则
      seg_entries 中会混有 SP 条目，导致映射错位（sp 条目被错误地
      当作可发音字符处理，后续的真实字符全部偏移一位）。

    **相邻 SP 合并**：_inject_sentence_pauses 可能已在标点处插入了小 SP
    （40/80ms），本函数截断后可能在同位置紧接着再产生一条 SP，两条
    相邻 SP 会被 _refine_sil_boundaries_by_energy 直接跳过（该函数有
    "prev_t == sil_mark 则 continue"的保护）。这里在第二步将所有相邻
    SP 合并为一条，确保能量修正能正常扩展到真实静音区域。

    参数
    ----
    max_cjk_char_sec    : CJK 单字符时长上限（默认从设置页面读取，兜底 0.50s）
    max_cjk_particle_sec : 语气词时长上限（默认从设置页面读取，兜底 0.35s）
    max_en_word_sec     : 英文单词时长上限（默认从设置页面读取，兜底 1.20s）
    min_sp_sec          : 低于此值不插入 SP（默认从设置页面读取，兜底 0.15s）

    未显式传入的参数（None）会在这里实时从 app_settings 读取用户在设置页面
    配置的值；显式传参时（当前代码库内两处调用均未传参）仍以调用方传入
    的值为准，便于测试/脚本场景覆盖设置。
    """
    if not entries:
        return entries

    if None in (max_cjk_char_sec, max_cjk_particle_sec, max_en_word_sec, min_sp_sec):
        _tuning = _get_alignment_tuning()
        if max_cjk_char_sec is None:
            max_cjk_char_sec = _tuning["ctc_max_cjk_char_sec"]
        if max_cjk_particle_sec is None:
            max_cjk_particle_sec = _tuning["ctc_max_cjk_particle_sec"]
        if max_en_word_sec is None:
            max_en_word_sec = _tuning["ctc_max_en_word_sec"]
        if min_sp_sec is None:
            min_sp_sec = _tuning["ctc_min_sp_sec"]

    _SIL_SET = frozenset({_SIL_MARK, "sp", "sil", "pau", "spn"})
    is_cjk = int_lang in ("zh", "yue", "ja", "ko")

    # ── 第一步：扫描超长 token，截断并插入 SP 占位 ───────────────────────
    raw: List[Tuple[float, float, str]] = []
    clipped = 0

    for s, e, t in entries:
        dur     = e - s
        t_lower = (t or "").strip().lower()

        # 已是静音标记或零长度条目，原样保留
        if t_lower in _SIL_SET or dur <= 0:
            raw.append((s, e, t))
            continue

        # 选择当前 token 对应的时长上限
        if is_cjk:
            ceiling = (max_cjk_particle_sec
                       if t in _CTC_PARTICLES
                       else max_cjk_char_sec)
        else:
            ceiling = max_en_word_sec

        if dur > ceiling + min_sp_sec:
            clip_end = s + ceiling
            sp_dur   = e - clip_end
            raw.append((s, clip_end, t))
            if sp_dur >= min_sp_sec:
                raw.append((clip_end, e, _SIL_MARK))
            clipped += 1
            logger.debug(
                f"[ctc_fix] '{t}' {dur:.3f}s → {ceiling:.3f}s"
                f"{f' + SP {sp_dur:.3f}s' if sp_dur >= min_sp_sec else ' (SP 过短跳过)'}"
            )
        else:
            raw.append((s, e, t))

    if clipped:
        logger.info(f"[ctc_fix] 共修复 {clipped} 个 CTC 拉伸 token")

    # ── 第二步：合并相邻 SP 条目 ─────────────────────────────────────────
    # 保证 _refine_sil_boundaries_by_energy 能正常扩展边界（该函数遇到
    # 相邻两条 SP 会 continue 跳过）。
    merged: List[Tuple[float, float, str]] = []
    for entry in raw:
        s_e, e_e, t_e = entry
        if (merged
                and (merged[-1][2] or "").strip().lower() in _SIL_SET
                and (t_e or "").strip().lower() in _SIL_SET):
            # 扩展前一条 SP 的结束时间而不是新建条目
            merged[-1] = (merged[-1][0], e_e, _SIL_MARK)
        else:
            merged.append(entry)

    return merged


# ═════════════════════════════════════════════════════════════════════════════
# 4. 基类
# ═════════════════════════════════════════════════════════════════════════════

class AltAlignerBase:
    """
    所有替代对齐后端的公共基类。
    共享 MFAProcessor 的音素转换 / 后处理逻辑，通过 _word_entries_to_lab() 复用。
    """

    def __init__(self):
        from mfa_processor import MFAProcessor
        self._mfa = MFAProcessor()

    def align(self, audio_path: str, text: Optional[str], language: str,
              english_word_align: bool = False) -> Dict:
        raise NotImplementedError

    # ── 词语时间戳 → LAB（含静音间隙补全）────────────────────────────────
    def _word_entries_to_lab(
        self,
        word_entries: List[Tuple[float, float, str]],
        text: str,
        language: str,
        fill_silences: bool = False,
        english_word_align: bool = False,
    ) -> str:
        """
        将词语 / 字符级时间戳 → LAB 格式，复用 MFAProcessor 的音素转换逻辑。
        fill_silences=True 时自动在时间间隙中插入 SIL。
        english_word_align=True 时英语单词直接输出，不做 ARPABET 音素拆分。

        关于标点：
          Qwen3 不产生标点字符的对齐时间戳（标点不可发音）。
          _text_to_syllables() 在提取音素序列时也会忽略标点，因此参考文本中
          的标点不影响音素分布——只要参考文本的可发音字符数与 entries 数量一致即可。
          句末 / 句中的停顿由 fill_silences 根据时间间隙自动插入 SIL。
        """
        if not word_entries:
            return ""

        lang = _normalize_lang(language)

        # 字符数不匹配时提前警告（便于调试）
        # 注：entries_n 要排除我们自己插入的静音条目（_inject_sentence_pauses
        # 写入的 sil 标记），否则每次插入停顿都会触发一次误报式警告。
        if text and lang in ("zh", "yue", "ja", "ko"):
            spoken_n = _count_spoken_chars(text, lang)
            entries_n = sum(
                1 for _, _, w in word_entries
                if (w or "").strip().lower() not in self._mfa.SIL_PHONES
            )
            if spoken_n != entries_n:
                logger.warning(
                    f"[alt_aligners] 参考文本可发音字符数 {spoken_n} ≠ "
                    f"对齐条目数 {entries_n}。如出现音素偏移，请检查参考文本是否与音频一致。"
                )

        # 日语 / 韩语需要特殊处理
        if lang == "ja":
            lab = self._ja_entries_to_lab(word_entries, text)
            return _fill_silences_lab(lab) if fill_silences else lab
        if lang == "ko":
            lab = self._ko_entries_to_lab(word_entries, text, english_word_align=english_word_align)
            return _fill_silences_lab(lab) if fill_silences else lab

        word_tier = [_MI(s, e, w) for s, e, w in word_entries]
        phone_items: List[Tuple[int, int, str]] = []

        if lang in ("zh", "cmn"):
            lines = self._mfa._process_zh_words(word_tier, phone_items, text,
                                                 english_word_align=english_word_align)
        elif lang == "yue":
            lines = self._mfa._process_yue_words(word_tier, phone_items, text,
                                                  english_word_align=english_word_align)
        else:
            lines = self._mfa._process_en_words(word_tier, phone_items, text,
                                                 english_word_align=english_word_align)

        lines = self._mfa._apply_lab_postprocess(lines, lang)
        lab = "\n".join(lines)
        return _fill_silences_lab(lab) if fill_silences else lab

    def _ja_entries_to_lab(
        self,
        word_entries: List[Tuple[float, float, str]],
        text: str,
    ) -> str:
        sil_entries: List[Tuple[int, int, str]] = []
        spoken_entries: List[Tuple[float, float, str]] = []
        for s, e, ch in word_entries:
            ch = (ch or "").strip()
            if ch.lower() in self._mfa.SIL_PHONES:
                sil_entries.append((int(s * 10_000_000), int(e * 10_000_000), _SIL_MARK))
            else:
                spoken_entries.append((s, e, ch))

        # 过滤标点，得到真正参与假名转换的字符序列（保留原时间戳）
        char_entries = [
            (s, e, ch) for s, e, ch in spoken_entries
            if ch and not _is_cjk_punct(ch)
        ]

        # 【修复】下面的 Sudachi 分词 + 下标消费逻辑要求"1 entry = 1
        # 字符"。WhisperX 的逐字符对齐天然满足，但 Qwen3-ForcedAligner
        # 的 token 可能是多字符的完整汉字词/英文单词（如"日本語"
        # "English"各占 1 条 entry）。统一展开成单字符粒度，避免汉字/
        # 英文在 LAB 里消失（详见 _explode_to_single_char_entries 文档）。
        char_entries = _explode_to_single_char_entries(char_entries)

        try:
            from sudachipy import dictionary as _sudachi_dictionary  # noqa: F401
        except ImportError:
            lines = [
                f"{int(s*10_000_000)} {int(e*10_000_000)} {ch}"
                for s, e, ch in char_entries
            ]
        else:
            joined_text = "".join(ch for _, _, ch in char_entries)
            lines = []
            if joined_text:
                tok, mode = _get_sudachi_tokenizer()
                morphemes = tok.tokenize(joined_text, mode)

                # 把逐字符时间戳和 morpheme 切分对齐：按每个 morpheme 的
                # surface 长度，依次"消费"对应数量的原始字符时间戳。
                idx = 0  # char_entries 游标
                for m in morphemes:
                    surface = m.surface()
                    n = len(surface)
                    if n <= 0:
                        continue
                    piece = char_entries[idx: idx + n]
                    idx += n
                    if not piece:
                        continue
                    reading_kata = m.reading_form() or surface
                    reading_hira = _kata_to_hira(reading_kata)

                    if n == 1:
                        # 单字符词：该字符自身只有一个时间戳，但读音可能不止
                        # 1 个 mora（如「僕」→ ぼく、「本」→ ほん，1 字 2
                        # mora）。统一交给 mora_list 分支判断，不再无条件
                        # 把整段读音直接塞进这一个时间戳——这正是此前「僕」
                        # 「本」等词被输出成单条 LAB 行（而非逐 mora）的根因。
                        mora_list = _split_ja_mora(reading_hira) if reading_hira else []
                        if not mora_list:
                            s, e, _ch = piece[0]
                            lines.append(
                                f"{int(s*10_000_000)} {int(e*10_000_000)} "
                                f"{reading_hira or surface}"
                            )
                        elif len(mora_list) == 1:
                            s, e, _ch = piece[0]
                            lines.append(
                                f"{int(s*10_000_000)} {int(e*10_000_000)} {mora_list[0]}"
                            )
                        else:
                            lines.extend(_distribute_mora_across_chars(piece, mora_list))
                    else:
                        # 多字符词（如「今日」「大変」）：字数与 mora 数恰好
                        # 相等时逐字直接对应（最常见、最精确）；不相等时
                        # （典型如「大変」2 字对应 たいへん 4 mora）由
                        # _distribute_mora_across_chars() 把 mora 尽量均匀
                        # 分配给各字符、再在每个字符自身时间戳内部按 mora
                        # 数等分，不再退回"整词合并成一条"的旧兜底。
                        mora_list = _split_ja_mora(reading_hira) if reading_hira else []
                        if not mora_list:
                            s = piece[0][0]
                            e = piece[-1][1]
                            lines.append(
                                f"{int(s*10_000_000)} {int(e*10_000_000)} "
                                f"{reading_hira or surface}"
                            )
                        elif len(piece) == len(mora_list):
                            for (ps, pe, _pch), mora in zip(piece, mora_list):
                                lines.append(
                                    f"{int(ps*10_000_000)} {int(pe*10_000_000)} {mora}"
                                )
                        else:
                            lines.extend(_distribute_mora_across_chars(piece, mora_list))

                # 极端情况下 tokenize 输出的字符总数与输入不符（理论上
                # 不应发生，但做个兜底，避免静默丢字）。
                if idx < len(char_entries):
                    for s, e, ch in char_entries[idx:]:
                        lines.append(f"{int(s*10_000_000)} {int(e*10_000_000)} {ch}")

        from mfa_processor import MFAProcessor
        entries_p = MFAProcessor._parse_lab_lines(lines)
        from phoneme_converter import merge_lab_silence
        # 注意：此处不再调用 build_ja_hiragana_lab()——sudachi 已经直接给出
        # 最终假名读音，不是需要状态机合并的单个罗马音素。二次转换会把每条
        # 假名读音误判为"待合并辅音声母"全部拆成 '-'，再被 merge_lab_silence()
        # 当作孤立辅音声母删除，导致最终 LAB 只剩 sil（见上方修复说明）。

        # 把摘出去的静音条目按时间顺序插回——状态机全程没见过它们，
        # 不会把它们的区间错误地并入相邻假名音节。
        combined = sorted(entries_p + sil_entries, key=lambda x: x[0])
        merged = merge_lab_silence(combined)
        return "\n".join(f"{s} {e} {p}" for s, e, p in merged)

    def _ko_entries_to_lab(
        self,
        word_entries: List[Tuple[float, float, str]],
        text: str,
        english_word_align: bool = False,
    ) -> str:
        lines: List[str] = []
        for s, e, ch in word_entries:
            ch = ch.strip()
            if not ch:
                continue
            s100 = int(s * 10_000_000)
            e100 = int(e * 10_000_000)

            if self._mfa._is_korean_text(ch):
                ko_only = "".join(c for c in ch if self._mfa._is_korean_text(c))
                if not ko_only:
                    continue
                syllable_entries = self._mfa._decompose_korean_syllable_with_onset(
                    s100, e100, ko_only, phone_items=None
                )
                for se, ee, pe in syllable_entries:
                    lines.append(f"{se} {ee} {pe}")
            else:
                if re.match(r"^[a-zA-Z\'\'-]+$", ch.strip()):
                    if english_word_align:
                        # 英语单词级对齐：直接输出单词，不做 ARPABET 拆分
                        lines.append(f"{s100} {e100} {ch.lower()}")
                    else:
                        from phoneme_converter import word_to_arpabet, distribute_arpabet_phones
                        g2p_phones = word_to_arpabet(ch)
                        if g2p_phones:
                            for ps, pe, pp in distribute_arpabet_phones(s100, e100, g2p_phones):
                                lines.append(f"{ps} {pe} {pp}")
                        else:
                            logger.warning(f"[ko/alt] 英语词 '{ch}' G2P 未命中，按整词输出")
                            lines.append(f"{s100} {e100} {ch.lower()}")
                else:
                    lines.append(f"{s100} {e100} {ch}")

        from mfa_processor import MFAProcessor
        entries_p = MFAProcessor._parse_lab_lines(lines)
        from phoneme_converter import merge_lab_silence
        merged = merge_lab_silence(entries_p)
        return "\n".join(f"{s} {e} {p}" for s, e, p in merged)
    def _get_audio_duration_100ns(self, audio_path: str) -> int:
        return self._mfa._get_audio_duration(audio_path)

# ═════════════════════════════════════════════════════════════════════════════
# 5. WhisperXAligner
# ═════════════════════════════════════════════════════════════════════════════

class WhisperXAligner(AltAlignerBase):
    """
    WhisperX 对齐后端（自动语音识别 + wav2vec2 强制音素对齐）
    https://github.com/m-bain/whisperx

    优势：
      - 不需要参考文本（自动转录模式）
      - 字符级对齐（中日韩），词语级对齐（英语等）
      - 支持 GPU 加速（CUDA）

    注意：
      - 结构化文本（编号/列表/Markdown 标题）会破坏"单调时间映射假设"，
        导致 wav2vec2 对齐失败。本类在调用对齐前自动调用
        normalize_text_for_whisperx() 进行口语化清洗。
      - 安装：pip install whisperx
    """

    # 当前支持的 Whisper 模型列表（由前端选择器引用）
    SUPPORTED_MODELS: List[str] = [
        "large-v3",
        "large-v3-turbo",
        "large-v2",
        "medium",
        "small",
        "base",
        "tiny",
    ]

    def __init__(
        self,
        whisper_model: str = "large-v3",
        device: str = "auto",
        compute_type: str = "float16",
        batch_size: int = 16,
        hf_token: Optional[str] = None,
        min_phoneme_dur: float = 0.025,   # PDG 最小音素时长（秒），25ms
    ):
        super().__init__()
        self.whisper_model = whisper_model
        self._device = self._resolve_device(device)
        # CPU 不支持 float16；GPU 还需检查实际硬件能力
        self.compute_type = self._resolve_compute_type(compute_type, self._device)
        self.batch_size = batch_size
        self.hf_token = hf_token or os.environ.get("HF_TOKEN")
        self.min_phoneme_dur = min_phoneme_dur

        self._asr_model = None
        self._align_models: Dict[str, object] = {}   # {lang_code: (model_a, metadata)}

    # ── 类方法 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _resolve_device(device: str) -> str:
        # _safe_device() 包含 smoke-test，避免 CPU-only torch 误选 CUDA
        # （torch.cuda.is_available() 在驱动存在时可能返回 True，但实际无法使用）
        return _safe_device(device)

    @staticmethod
    def _resolve_compute_type(compute_type: str, device: str) -> str:
        """
        根据设备和 GPU 能力自动选择最优 compute_type，避免运行时 ValueError。

        优先级（CUDA）：用户指定 → GPU 能力检测 → int8 保底
        - float16 需要 Tensor Core（NVIDIA compute capability ≥ 7.0，即 Turing+）
        - 旧卡（Pascal / Maxwell 等）只能用 int8 或 float32
        """
        if device == "cpu":
            # CPU 后端：int8 最快，float32 最兼容
            return "int8" if compute_type in ("float16", "int8_float16") else compute_type

        if compute_type not in ("float16", "int8_float16"):
            # 用户显式指定了 int8 / float32 等，直接信任
            return compute_type

        # 尝试通过 ctranslate2（faster-whisper 依赖）查询 GPU 支持的精度列表
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
            # ctranslate2 未安装或查询失败 → 保守回退到 int8
            logger.warning(
                f"[WhisperX] 无法查询 GPU compute_type 支持情况，"
                f"保守切换: {compute_type} → int8"
            )
            return "int8"

    @staticmethod
    def check_available() -> Tuple[bool, str]:
        try:
            import whisperx  # noqa: F401
            return True, "OK"
        except ImportError as e:
            return False, f"未安装: pip install whisperx ({e})"
        except Exception as e:
            return False, str(e)

    # ── 懒加载 ──────────────────────────────────────────────────────────────
    def _load_asr(self):
        if self._asr_model is not None:
            return

        import whisperx

        # 按优先级构建 compute_type 尝试链：
        #   float16 → int8 → float32（越来越保守，最后一个必定成功）
        _FALLBACK: Dict[str, list] = {
            "float16":       ["int8", "float32"],
            "int8_float16":  ["int8", "float32"],
            "int8":          ["float32"],
        }
        candidates = [self.compute_type] + _FALLBACK.get(self.compute_type, [])

        last_exc: Optional[Exception] = None
        for ct in candidates:
            try:
                logger.info(
                    f"[WhisperX] 加载 ASR 模型: {self.whisper_model} "
                    f"({self._device}, compute_type={ct})"
                )
                self._asr_model = whisperx.load_model(
                    self.whisper_model,
                    self._device,
                    compute_type=ct,
                    download_root=str(_WHISPER_CACHE),
                )
                if ct != self.compute_type:
                    logger.warning(
                        f"[WhisperX] compute_type 自动降级: "
                        f"{self.compute_type} → {ct}（GPU 不支持高精度浮点）"
                    )
                    self.compute_type = ct
                logger.info(f"[WhisperX] ✓ ASR 模型已加载 (compute_type={ct})")
                return
            except ValueError as e:
                err_lower = str(e).lower()
                if "compute type" in err_lower or "float16" in err_lower:
                    logger.warning(f"[WhisperX] compute_type={ct} 失败: {e}，尝试下一档...")
                    last_exc = e
                else:
                    raise  # 非精度相关错误，直接抛出

        # 所有候选均失败
        raise last_exc or RuntimeError("[WhisperX] 所有 compute_type 均失败，请检查 GPU 驱动")

    def _load_align(self, lang_code: str):
        if lang_code not in self._align_models:
            import whisperx
            logger.info(f"[WhisperX] 加载对齐模型: {lang_code}")
            model_a, metadata = whisperx.load_align_model(
                language_code=lang_code, device=self._device
            )
            self._align_models[lang_code] = (model_a, metadata)
            logger.info(f"[WhisperX] ✓ 对齐模型 ({lang_code}) 已加载")
        return self._align_models[lang_code]

    # ── 粗测（仅 ASR 转录，不做 wav2vec2 强制对齐）─────────────────────────
    def _transcribe_rough_segments(self, audio_path: str, language: str) -> Dict:
        """
        仅做一次 ASR 转录，拿到 Whisper 自身 VAD 分段给出的句级"粗略"
        时间戳——不含 align() 后续的逐句裁剪 + wav2vec2 强制对齐 + 静音
        精修等步骤，因此比完整的 align() 快得多。

        专供 Qwen3ForcedAligner 的长音频分段规划复用（详见模块下方
        _plan_chunks_via_whisperx_rough_pass 顶部说明）：Qwen3-FA 自己的
        分段对齐此前完全依赖"假设语速均匀、按参考文本字符数占比反推
        每句在全曲时间轴上的位置"这一估算方式，在演唱/拖腔/语速不均
        的素材上系统性误差可达 1~2 秒，导致喂给 Qwen3-FA 的每一段物理
        边界本身就没卡准，即使 Qwen3-FA 自己对这一段内部再怎么对齐也
        无法弥补，表现为大量"自愈修复/均匀分配"退化兜底。这里借用
        WhisperX（真实 ASR，不依赖字符比例假设）的分段结果作为更可靠的
        边界来源；只用它的 (start, end) 时间戳和"这一段自己识别出多少
        字"这两个信息，从不使用它识别出的文字内容本身——真正喂给
        Qwen3-FA 做精细对齐的文本，仍然是原始参考文本按字数配额切出的
        一个切片（见 _bind_ref_text_by_asr_count），保证不引入 ASR 识别
        错误。

        Returns
        -------
        {"success": True, "raw_segments": [...]}：每个元素至少含
        "start"/"end"/"text" 三个键（whisperx transcribe() 原始输出格式；
        "text" 是 Whisper 自己的识别文本，仅用于计算字数配额，不会被
        当作最终对齐文本使用）。
        失败（whisperx 未安装 / 音频加载失败 / ASR 无输出）时返回
        {"success": False, "error": "..."}，调用方应无缝回退到旧的按
        字符比例估算方案，不让整个对齐任务失败。
        """
        try:
            import whisperx
        except ImportError as e:
            return {"success": False, "error": f"whisperx 未安装: {e}"}

        try:
            wx_lang = _to_whisperx_lang(language)

            # 音频加载：与 align() 里的加载逻辑独立实现（而不是共用同一个
            # 私有方法），刻意不改动已经过验证的 align() 本身，把这条新增
            # 路径的风险完全隔离在这个新方法内部——即使这里出现任何问题，
            # 也不会影响 WhisperX 作为独立对齐后端时的既有行为。
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message=".*torchcodec.*")
                    audio = whisperx.load_audio(audio_path)
            except Exception as _ffmpeg_err:
                try:
                    import soundfile as _sf
                    import numpy as _np
                    _data, _orig_sr = _sf.read(audio_path, always_2d=False)
                    if _data.ndim > 1:
                        _data = _data.mean(axis=1)
                    _data = _data.astype(_np.float32)
                    if _orig_sr != 16_000:
                        import librosa as _librosa
                        _data = _librosa.resample(_data, orig_sr=_orig_sr, target_sr=16_000)
                    audio = _data
                except Exception as _sf_err:
                    return {
                        "success": False,
                        "error": f"音频加载失败 (ffmpeg: {_ffmpeg_err}; soundfile: {_sf_err})",
                    }

            self._load_asr()
            asr_out = self._transcribe_with_oom_retry(audio, wx_lang)
            raw_segments = asr_out.get("segments", [])
            if not raw_segments:
                return {"success": False, "error": "WhisperX ASR 无输出，请检查音频质量"}
            return {"success": True, "raw_segments": raw_segments}
        except Exception as e:
            logger.warning(f"[WhisperX][粗测] ASR 转录失败: {e}")
            return {"success": False, "error": str(e)}

    def _transcribe_with_oom_retry(self, audio, wx_lang: str) -> Dict:
        """
        对 self._asr_model.transcribe() 的一层 batch_size 自适应重试封装：
        遇到 CUDA 显存不足时自动腰斩 batch_size 重试，直到 batch_size=1
        仍然失败才真正把异常抛给调用方。

        背景：WhisperXAligner 是跨任务复用的单例（见 get_aligner() 的
        缓存），"这次转录用多大的 batch_size"如果只依赖 __init__ 时保存
        的 self.batch_size，用户在设置页面调完 whisperx_batch_size 之后，
        已经创建好的单例不会重新构造，改了也不会生效。这里每次调用都
        实时读取最新的 whisperx_batch_size 设置（与其余对齐调优参数
        "保存后下一次任务立即生效、无需重启"的约定一致）。

        低显存卡（比如 6GB 的 P106-100 这类老卡，尤其是显存本身已经被
        其他进程占用一部分时）在默认 batch_size=16 下很容易在 ASR 转录
        阶段直接 CUDA OOM；自动腰斩重试可以让同一次任务在大多数情况下
        不需要用户手动调小设置、重新提交就能跑完，只是会慢一些——真正
        每次都在 batch_size=1 也放不下时，才说明是模型本身（而不是
        batch_size）对这块显卡来说太大了，此时会抛出一条明确指出"降低
        whisperx_batch_size 或换更小模型"的错误，而不是把原始难懂的
        CUDA 报错直接抛给用户。
        """
        batch_size = _get_whisperx_batch_size()
        last_exc: Optional[Exception] = None
        while batch_size >= 1:
            try:
                return self._asr_model.transcribe(
                    audio, batch_size=batch_size, language=wx_lang
                )
            except RuntimeError as e:
                if "out of memory" not in str(e).lower():
                    raise
                last_exc = e
                logger.warning(
                    f"[WhisperX] ASR 转录 CUDA 显存不足（batch_size={batch_size}），"
                    "尝试释放显存缓存并腰斩 batch_size 重试…"
                )
                try:
                    import torch as _torch_oom
                    if _torch_oom.cuda.is_available():
                        _torch_oom.cuda.empty_cache()
                except Exception:
                    pass
                if batch_size == 1:
                    break
                batch_size = max(1, batch_size // 2)

        raise RuntimeError(
            f"CUDA 显存不足，即使把 batch_size 降到 1 仍然失败——当前 GPU "
            f"剩余显存可能已经不够运行 {self.whisper_model} 模型本身（与"
            "这一条音频具体多长关系不大）。建议在设置里把 "
            "whisperx_batch_size 调得更小，或把使用的 Whisper 模型档位"
            "换成更小的（medium / small / base），也可以检查一下是否有"
            f"其他进程占用了显存。原始错误: {last_exc}"
        )

    # ── 核心对齐（句子隔离版）────────────────────────────────────────────────
    def align(self, audio_path: str, text: Optional[str], language: str,
              english_word_align: bool = False) -> Dict:
        """
        句子隔离强制对齐（Sentence-Isolated Alignment）。

        改进点（对比旧版）：
          1. 逐句裁剪音频 → 在极短时序空间内单独对齐，消除长文本累计漂移。
          2. 参考文本与 ASR 句数匹配时，将参考文本绑定到对应句子（修正繁简/识别错误）。
          3. 每句独立完成 LAB 转换，避免字符数不一致导致的全局偏移。
          4. 音素时长守护（PDG）消除极短音标（< 25ms）。
          5. 【已修复】不再是"全程 fill_silences=False，输出零 SP/SIL 的纯净
             连续音标序列"——wav2vec2 在句子内部几乎不会留出时间间隙，旧版
             仅靠 fill_silences 做不到任何停顿。现在改为 _inject_sentence_pauses()
             在标点位置主动插入真正的 sil 条目（见该函数顶部说明），fill_silences
             本身仍为 False（句内不需要按"时间间隙"再做一遍全局扫描，标点
             位置已经显式处理）。
          6. 【已修复】上一步插入的 sil 只有固定的 40/80ms，远短于实际录音
             里的换气/停顿（wav2vec2 经常把真实静音错误地算进标点前最后
             一个字的时长里，实测可达 200~400ms+）。现在加一步
             _refine_sil_boundaries_by_energy()，用该句真实裁剪音频的短时
             能量扫描，把 sil 边界扩展到真正安静的区域，让停顿长度跟随
             这一句实际演唱内容，而不是停在一个固定值上（详见该函数顶部
             说明）。
        """
        t0 = time.time()
        try:
            import whisperx

            wx_lang  = _to_whisperx_lang(language)
            int_lang = _normalize_lang(language)
            _SR      = 16_000   # WhisperX load_audio 固定输出 16kHz

            # ── 1. 加载音频 ──────────────────────────────────────────────────
            # whisperx.load_audio() 依赖 ffmpeg 子进程；若环境中 ffmpeg 不可用
            # 则回退到 soundfile（直接读 WAV/FLAC）+ librosa 重采样，避免崩溃。
            try:
                import warnings
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message=".*torchcodec.*",   # 屏蔽 pyannote torchcodec 警告
                    )
                    audio = whisperx.load_audio(audio_path)   # float32 numpy, 16kHz
            except Exception as _ffmpeg_err:
                logger.warning(
                    f"[WhisperX] whisperx.load_audio 失败（{_ffmpeg_err}），"
                    "尝试用 soundfile + librosa 回退加载…"
                )
                try:
                    import soundfile as _sf
                    import numpy as _np
                    _data, _orig_sr = _sf.read(audio_path, always_2d=False)
                    if _data.ndim > 1:
                        _data = _data.mean(axis=1)   # 混音为单声道
                    _data = _data.astype(_np.float32)
                    if _orig_sr != _SR:
                        import librosa as _librosa
                        _data = _librosa.resample(_data, orig_sr=_orig_sr, target_sr=_SR)
                    audio = _data
                    logger.info(
                        f"[WhisperX] soundfile 回退加载成功: "
                        f"{len(audio)/float(_SR):.2f}s @ {_SR}Hz"
                    )
                except Exception as _sf_err:
                    return self._err(
                        f"音频加载失败 (ffmpeg: {_ffmpeg_err}; soundfile: {_sf_err})。"
                        "请在系统 PATH 中安装 FFmpeg，或确保 soundfile 已安装。",
                        t0,
                    )

            # ── 2. ASR 转录（仅用于获取句子级时序边界）──────────────────────
            self._load_asr()
            logger.info("[WhisperX] 开始 ASR 转录...")
            asr_out      = self._transcribe_with_oom_retry(audio, wx_lang)
            raw_segments = asr_out.get("segments", [])
            if not raw_segments:
                return self._err("WhisperX ASR 无输出，请检查音频质量", t0)

            asr_text_full = " ".join(s.get("text", "") for s in raw_segments).strip()
            logger.info(f"[WhisperX] ASR 文本: {asr_text_full[:120]}")
            logger.info(f"[WhisperX] ASR 共检出 {len(raw_segments)} 句")

            # ── 3. 参考文本预处理：断句并与 ASR 句段绑定 ────────────────────
            #    句数完全一致时，按句直接绑定；
            #    句数不一致（绝大多数情况）时，按"每段 ASR 自己的识别字数"
            #    为配额，把参考文本（保留标点）顺序切给各段——配额用 ASR
            #    字数而不是音频时长，是为了保证替换后送进 wav2vec2 对齐的
            #    字符数不变，不会导致强制对齐被迫拉伸/压缩造成时间戳错位。
            if text:
                cleaned_ref  = normalize_text_for_whisperx(text, lang=int_lang)
                ref_sentences: List[str] = [
                    s.strip()
                    for s in _re.split(r'[。！？；\n…!?]+', cleaned_ref)
                    if s.strip()
                ]
                if len(ref_sentences) == len(raw_segments):
                    logger.info(
                        f"[WhisperX] 参考文本句数 {len(ref_sentences)} == ASR 句段数，"
                        "绑定参考文本 → 每句使用参考文本对齐"
                    )
                    for i, seg in enumerate(raw_segments):
                        seg["text"] = ref_sentences[i]
                else:
                    bound = _bind_ref_text_by_asr_count(cleaned_ref, raw_segments, int_lang)
                    if bound:
                        logger.warning(
                            f"[WhisperX] 参考文本切出 {len(ref_sentences)} 句 ≠ "
                            f"ASR 段数 {len(raw_segments)}，按各段 ASR 识别字数为配额"
                            "分配参考文本（保留标点，字数严格对应，不退回 ASR 文本）"
                        )
                    else:
                        logger.warning(
                            f"[WhisperX] 参考文本切出 {len(ref_sentences)} 句 ≠ "
                            f"ASR 段数 {len(raw_segments)}，保留 ASR 识别文本逐句对齐"
                        )

            # ── 4. 加载对齐模型 ──────────────────────────────────────────────
            model_a, metadata = self._load_align(wx_lang)
            logger.info(f"[WhisperX] 开始逐句隔离强制对齐（共 {len(raw_segments)} 句）...")

            # ── 5. 句子隔离强制对齐核心循环 ──────────────────────────────────
            #    对每句：① 物理裁剪音频 → ② 在局部短时序空间内对齐
            #          → ③ 局部时间戳 + 句子偏移 = 全局绝对时间戳
            #    完全消除跨句累计漂移和 CTC 路径崩溃导致的音标粘连。
            # seg_pair_list: [(entries_for_this_seg, text_for_this_seg), ...]
            seg_pair_list: List[Tuple[List[Tuple[float, float, str]], str]] = []

            for idx, seg in enumerate(raw_segments):
                start_sec = float(seg.get("start", 0.0))
                end_sec   = float(seg.get("end",   0.0))
                seg_text  = seg.get("text", "").strip()

                if not seg_text or end_sec <= start_sec:
                    continue

                # 物理裁剪：提取该句的音频片段
                st_samp = max(0, int(start_sec * _SR))
                en_samp = min(len(audio), int(end_sec   * _SR))
                cropped = audio[st_samp:en_samp]

                if len(cropped) < 160:      # < 10ms，跳过
                    logger.warning(
                        f"[WhisperX] 第 {idx+1} 句裁剪后过短（{len(cropped)} samples），跳过"
                    )
                    continue

                # 对齐模型接受的文本：剥离标点符号（，。！？：等传入会
                # 导致 wav2vec2 词表缺失而跳过整句），但保留空白和单词
                # 内部撇号——前者是英语/韩语等多词语言的词边界，被误删
                # 会导致整句被拼接成一个伪单词，wav2vec2 只能返回 1 个
                # 跨越全句的 word 条目（详见 _clean_align_text() 顶部的
                # bug 说明）。改用 _clean_align_text()，不再用
                # _is_cjk_punct() 逐字符过滤（该函数把空白也判定为
                # "标点"一并清除）。
                seg_text_for_align = _clean_align_text(seg_text)
                if not seg_text_for_align:
                    continue

                # 单句任务：局部时间从 0 开始
                local_seg_list = [{"text": seg_text_for_align, "start": 0.0, "end": end_sec - start_sec}]

                seg_entries: List[Tuple[float, float, str]] = []
                try:
                    local_aligned = whisperx.align(
                        local_seg_list, model_a, metadata, cropped, self._device,
                        return_char_alignments=True,   # CJK 字符级对齐
                    )

                    for a_seg in local_aligned.get("segments", []):
                        # 中/粤/日/韩都按字符级（chars）切分：中日粤本身
                        # 不用空格分词；韩语虽然书写时用空格分隔"词"
                        # （어절），但歌唱场景下需要的是逐音节字符级时间戳
                        # （和中文逐字一致），不是整个词组一条目——
                        # _ko_entries_to_lab() 早就实现了逐字符的韩语
                        # 处理（含初声"-"占位拆分），此前却一直走 else
                        # 分支取 words（词组级），导致该函数从未真正吃到
                        # 单字符输入，对齐结果停留在"整句/整词组一条目"。
                        if int_lang in ("zh", "yue", "ja", "ko"):
                            units    = a_seg.get("chars", [])
                            text_key = "char"
                        else:
                            units    = a_seg.get("words", [])
                            text_key = "word"

                        for unit in units:
                            s = unit.get("start")
                            e = unit.get("end")
                            t = (unit.get(text_key) or unit.get("text") or "").strip()
                            if s is None or e is None or not t or _is_cjk_punct(t):
                                continue
                            # 局部时间 → 全局绝对时间
                            seg_entries.append(
                                (float(s) + start_sec, float(e) + start_sec, t)
                            )

                except Exception as exc:
                    logger.error(
                        f"[WhisperX] 第 {idx+1} 句对齐异常（'{seg_text[:30]}'）: {exc}"
                    )
                    # 降级：整句作为单一条目，保持时间轴不断裂
                    seg_entries = [(start_sec, end_sec, seg_text)]

                # 【修复】中/粤/韩走的是字符级（chars）对齐，若参考文本里混有
                # 英文单词（如中文歌词夹的 "Singing"），WhisperX 会把该单词
                # 也拆成一个个孤立字母，每个字母单独命中"是否为英文词"的
                # 正则后被当成"独立单词"送进 G2P，得到的是字母拼读音
                # （S→ess, G→gee...）而不是单词本身的发音。这里在送入
                # _process_zh_words()/_process_yue_words()/_ko_entries_to_lab()
                # 之前，把时间连续的单字母 entries 重新拼回完整英文单词。
                #
                # 注意：唯独日语 (ja) 不能做这个合并。_ja_entries_to_lab()
                # 在 sudachipy 可用时会把 char_entries 拼成 joined_text 交给
                # Sudachi 分词，再按每个 morpheme 的 surface 字符长度从
                # char_entries 里"消费"对应数量的条目——这是"1 个字面字符
                # = 1 个 entry"的强假设。先把字母合并成单词会让 surface 长度
                # （字面字符数，不受合并影响）与可消费的 entries 数量（合并后
                # 变少）不一致，导致该单词之后所有字符的时间戳全部错位。
                # zh/yue 的 _process_*_words() 和 ko 的 _ko_entries_to_lab()
                # 都是逐条独立处理每个 entry（不做基于字符位置的索引消费），
                # 合并对它们是安全的。
                if int_lang in ("zh", "yue", "ko"):
                    seg_entries = _merge_latin_letter_chars(seg_entries)

                # 句内标点停顿注入：在标点对应位置插入真正的 sil 条目
                # （详见 _inject_sentence_pauses 顶部说明），条目数量会
                # 因此增多，但不改变原有发音字符的相对顺序，不会引入
                # 新的拼音/音节错位。
                seg_entries = _inject_sentence_pauses(seg_entries, seg_text)

                # 【CTC 拉伸修复】WhisperX wav2vec2 CTC blank frame 无"强制
                # 停顿"概念，会把短语边界换气/停顿处的 blank frame 全部分配给
                # 上一个 token，导致短语末尾字符时长被严重拉长（实测 1.2s～
                # 1.7s）。_inject_sentence_pauses 只处理有标点标记的位置且
                # 上限仅 40/80ms，无法解决无标点的呼气停顿拉伸问题。
                # 此步截断超限 token 并插入 SP 占位，供下一步能量修正扩展到
                # 真实静音边界（详见 _fix_ctc_stretch 顶部说明）。
                # 注意：必须在 _inject_sentence_pauses 之后调用，避免破坏
                # 该函数按字符索引映射 gap_after 的内部逻辑。
                seg_entries = _fix_ctc_stretch(seg_entries, int_lang)

                # 上一步给的 40/80ms 只是"至少要有多长停顿"的下限，
                # 不代表音频里真实的换气/停顿就这么短——wav2vec2 经常
                # 把真正的静音错误地算进标点前最后一个字的时长里。这里
                # 用该句裁剪出来的真实音频（cropped，16kHz）做短时能量
                # 扫描，把 sil 边界扩展到真正安静的区域（详见函数顶部
                # 说明），让 SVP 里的停顿长度跟到这一句实际演唱的换气
                # 时长，而不是一个跟内容无关的固定值。
                seg_entries = _refine_sil_boundaries_by_energy(
                    seg_entries, cropped, _SR, start_sec
                )

                if seg_entries:
                    seg_pair_list.append((seg_entries, seg_text))

            if not seg_pair_list:
                return self._err("所有句子对齐均失败，请检查音频质量和语言设置", t0)

            # ── 6. 音素时长守护（PDG）──────────────────────────────────────
            #    每句内部独立运行，将 < min_phoneme_dur 的极短音标扩展到安全时长。
            #    句间间隙（说话停顿）不受影响，总时长严格守恒。
            guarded_pair_list: List[Tuple[List[Tuple[float, float, str]], str]] = []
            for seg_entries, seg_text in seg_pair_list:
                guarded = self._apply_duration_guard(seg_entries, self.min_phoneme_dur)
                guarded_pair_list.append((guarded, seg_text))

            # ── 7. 逐句转换为 LAB（标点处含真实 sil 条目）──────────────────────
            #    每句独立调用 _word_entries_to_lab，用当句文本驱动音素转换，
            #    彻底杜绝字符数不一致跨句传播的偏移错误。fill_silences=False
            #    是因为句内停顿已经由上面第 5 步的 _inject_sentence_pauses()
            #    显式写入了 sil 条目，不需要再做一次基于时间间隙的全局扫描。
            lab_blocks: List[str] = []
            for seg_entries, seg_text in guarded_pair_list:
                if not seg_entries:
                    continue
                block = self._word_entries_to_lab(
                    seg_entries, seg_text, language, fill_silences=False,
                    english_word_align=english_word_align
                )
                if block.strip():
                    lab_blocks.append(block)

            lab = "\n".join(lab_blocks)

            return {
                "success": True,
                "lab_content": lab,
                "raw_text":     text.strip() if text else asr_text_full,
                "phoneme_text": asr_text_full,
                "audio_duration": self._get_audio_duration_100ns(audio_path),
                "processing_time": int((time.time() - t0) * 1000),
                "backend": "whisperx",
            }

        except ImportError as e:
            return self._err(f"whisperx 未安装: {e}，请执行 pip install whisperx", t0)
        except Exception as e:
            logger.error(f"[WhisperX] 对齐失败: {e}", exc_info=True)
            return self._err(str(e), t0)

    # ── 音素时长守护算法（PDG）────────────────────────────────────────────────
    @staticmethod
    def _apply_duration_guard(
        entries: List[Tuple[float, float, str]],
        min_dur_sec: float = 0.025,
    ) -> List[Tuple[float, float, str]]:
        """
        音素时长守护（Phoneme Duration Guard, PDG）。

        对时长 < min_dur_sec 的极短音标，采用双向邻近贪心借用算法进行扩展：
          - 向左右邻居各借用一半时差，邻居自身不低于 min_dur_sec；
          - 单侧不足时由另一侧补足；
          - 首/尾条目仅向另一侧借用；
          - 修正浮点精度导致的边界倒置。
        全局总时长严格守恒，句首/句尾绝对时间不改变。
        """
        if not entries:
            return entries

        es = [[s, e, t] for s, e, t in entries]
        n  = len(es)

        for i in range(n):
            dur = es[i][1] - es[i][0]
            if dur >= min_dur_sec:
                continue

            deficit  = min_dur_sec - dur
            is_first = (i == 0)
            is_last  = (i == n - 1)

            if is_first and is_last:
                # 单条目：强制拉伸右边界
                es[i][1] = es[i][0] + min_dur_sec

            elif is_first:
                # 首部：仅向右借
                avail  = max(0.0, (es[i+1][1] - es[i+1][0]) - min_dur_sec)
                borrow = min(deficit, avail)
                es[i][1]   += borrow
                es[i+1][0] += borrow

            elif is_last:
                # 末尾：仅向左借
                avail  = max(0.0, (es[i-1][1] - es[i-1][0]) - min_dur_sec)
                borrow = min(deficit, avail)
                es[i-1][1] -= borrow
                es[i][0]   -= borrow

            else:
                # 中间：双向对称借用，不足时由另一侧补足
                l_avail  = max(0.0, (es[i-1][1] - es[i-1][0]) - min_dur_sec)
                r_avail  = max(0.0, (es[i+1][1] - es[i+1][0]) - min_dur_sec)
                b_left   = min(deficit / 2.0, l_avail)
                b_right  = min(deficit - b_left, r_avail)
                # 右边不足时左边再补
                if b_right < deficit - b_left:
                    extra   = (deficit - b_left - b_right)
                    b_left += min(extra, l_avail - b_left)
                    b_right = min(deficit - b_left, r_avail)

                es[i-1][1] -= b_left
                es[i][0]   -= b_left
                es[i][1]   += b_right
                es[i+1][0] += b_right

        # 修复浮点误差导致的相邻边界倒置
        for i in range(n - 1):
            if es[i][1] > es[i+1][0]:
                mid = (es[i][1] + es[i+1][0]) / 2.0
                es[i][1]   = mid
                es[i+1][0] = mid

        return [(s, e, t) for s, e, t in es]

    # ── 兼容性保留（旧版内部辅助方法，新版 align() 不再调用）────────────────
    def _extract_entries(
        self, aligned: Dict, int_lang: str
    ) -> List[Tuple[float, float, str]]:
        """[已弃用] 从全局 aligned 结果提取展平条目列表。"""
        entries: List[Tuple[float, float, str]] = []
        for seg in aligned.get("segments", []):
            chars = seg.get("chars", [])
            words = seg.get("words", [])
            if int_lang in ("zh", "yue", "ja") and chars:
                for ch in chars:
                    s = ch.get("start"); e = ch.get("end")
                    t = (ch.get("char") or ch.get("text") or "").strip()
                    if s is not None and e is not None and t and not _is_cjk_punct(t):
                        entries.append((float(s), float(e), t))
            elif words:
                for w in words:
                    s = w.get("start"); e = w.get("end")
                    t = (w.get("word") or w.get("text") or "").strip()
                    if s is not None and e is not None and t:
                        entries.append((float(s), float(e), t))
        entries.sort(key=lambda x: x[0])
        return entries

    def _extract_entries_per_segment(
        self, aligned: Dict, int_lang: str
    ) -> List[List[Tuple[float, float, str]]]:
        """[已弃用] 从全局 aligned 结果按 segment 提取条目列表。"""
        result: List[List[Tuple[float, float, str]]] = []
        for seg in aligned.get("segments", []):
            chars = seg.get("chars", []); words = seg.get("words", [])
            seg_e: List[Tuple[float, float, str]] = []
            if int_lang in ("zh", "yue", "ja") and chars:
                for ch in chars:
                    s = ch.get("start"); e = ch.get("end")
                    t = (ch.get("char") or ch.get("text") or "").strip()
                    if s is not None and e is not None and t and not _is_cjk_punct(t):
                        seg_e.append((float(s), float(e), t))
            elif words:
                for w in words:
                    s = w.get("start"); e = w.get("end")
                    t = (w.get("word") or w.get("text") or "").strip()
                    if s is not None and e is not None and t:
                        seg_e.append((float(s), float(e), t))
            seg_e.sort(key=lambda x: x[0])
            if seg_e:
                result.append(seg_e)
        return result

    def _segments_to_lab(
        self,
        seg_entries_list: List[List[Tuple[float, float, str]]],
        full_text: str,
        language: str,
    ) -> str:
        """[已弃用] 旧版逐段转 LAB，保留供外部调用兼容。"""
        if not seg_entries_list:
            return ""
        lang = _normalize_lang(language)
        if lang not in ("zh", "yue", "ja") or not full_text:
            flat = [e for seg in seg_entries_list for e in seg]
            return self._word_entries_to_lab(flat, full_text, language, fill_silences=False,
                                              english_word_align=english_word_align)
        spoken_chars = [
            ch for ch in full_text
            if not unicodedata.category(ch).startswith(("P", "Z", "S"))
        ]
        total_entries = sum(len(s) for s in seg_entries_list)
        if len(spoken_chars) != total_entries:
            logger.warning(
                f"[WhisperX] 参考文本可发音字符 {len(spoken_chars)} ≠ 条目数 {total_entries}，"
                "逐段用 ASR 字符独立转换（不再退化为全局展平）"
            )
            blocks = []
            for seg_entries in seg_entries_list:
                seg_text = "".join(t for _, _, t in seg_entries)
                b = self._word_entries_to_lab(seg_entries, seg_text, language, fill_silences=False,
                                          english_word_align=english_word_align)
                if b.strip():
                    blocks.append(b)
            return "\n".join(blocks)
        blocks = []
        cursor = 0
        for seg_entries in seg_entries_list:
            n = len(seg_entries)
            seg_text = "".join(spoken_chars[cursor:cursor + n])
            cursor += n
            b = self._word_entries_to_lab(seg_entries, seg_text, language, fill_silences=False,
                                      english_word_align=english_word_align)
            if b.strip():
                blocks.append(b)
        return "\n".join(blocks)

    @staticmethod
    def _err(msg: str, t0: float) -> Dict:
        return {
            "success": False,
            "error": msg,
            "processing_time": int((time.time() - t0) * 1000),
        }


# ═════════════════════════════════════════════════════════════════════════════
# 6. Qwen3ASRAligner
# ═════════════════════════════════════════════════════════════════════════════

class Qwen3ASRAligner(AltAlignerBase):
    """
    Qwen3-ASR 独立服务客户端
    只通过 HTTP 调用 qwen3_server.py，不在当前进程内加载模型。
    """

    DEFAULT_MODEL = "Qwen/Qwen3-ASR-1.7B"
    DEFAULT_ENDPOINT = "http://127.0.0.1:5001/asr"

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL,
        device: str = "auto",
        endpoint: str = DEFAULT_ENDPOINT,
        batch_size: int = 8,
    ):
        super().__init__()
        self.model_id = model_id
        self._device = device
        self.endpoint = endpoint.rstrip("/")
        self._session = None
        # 透传给 qwen3_server.py /asr 请求体里的 "batch_size" 字段，服务端
        # 据此设置 qwen_asr 官方的 max_inference_batch_size（见
        # qwen3_server.py load_model() 顶部说明）。默认 8，与
        # app_settings.DEFAULT_SETTINGS["qwen3_batch_size"] 一致。
        self.batch_size = max(1, int(batch_size))

    @staticmethod
    def check_available() -> Tuple[bool, str]:
        try:
            import requests  # noqa: F401
        except ImportError as e:
            return False, f"未安装 requests: pip install requests ({e})"

        try:
            r = requests.get("http://127.0.0.1:5001/", timeout=2)
            return True, "Qwen3-ASR 独立服务已可访问"
        except Exception as e:
            return False, f"Qwen3-ASR 独立服务不可访问: {e}"

    def _load_model(self):
        """
        独立服务模式下，不加载本地模型。
        这里只做轻量级连接初始化。
        """
        if self._session is None:
            self._session = requests.Session()

    def _call_qwen3_service(self, audio_path: str, language: str, context: str = "") -> Dict:
        self._load_model()

        payload = {
            "audio": audio_path,
            "language": language,
            "context": context,
            # 【修复】此前这里从未发送 "device" 字段，qwen3_server.py 的
            # /asr 路由因此永远按其内部默认值 "auto" 解析设备，导致用户在
            # "对齐工具运行设备"里选择 CPU 完全不会对 Qwen3-ASR 生效（与
            # pipeline.py 里 WhisperX/Qwen3-FA/NeMo-FA 曾经历过的
            # aligner_device 失效是同一类问题）。这里用 _safe_device() 在
            # 客户端（主进程）先做一次 CUDA 可用性 smoke-test 再传给独立
            # 服务进程，与 WhisperXAligner 的设备解析方式保持一致。
            "device": _safe_device(getattr(self, "_device", "auto")),
            "batch_size": self.batch_size,
        }

        resp = self._session.post(self.endpoint, json=payload, timeout=1800)
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success", False):
            raise RuntimeError(data.get("error", "Qwen3-ASR 服务返回失败"))

        return data

    @staticmethod
    def _flatten_segments_to_entries(segments, int_lang: str):
        """
        将独立服务返回的 segments 转成:
        [(start_sec, end_sec, text), ...]
        """
        entries = []

        for seg in segments or []:
            text = (seg.get("text") or "").strip()
            time_stamps = seg.get("time_stamps") or seg.get("timestamp") or []

            if not text:
                continue

            # 兼容几种返回形式：
            # 1) [[s, e], [s, e], ...]
            # 2) [{"start": s, "end": e, "text": "x"}, ...]
            # 3) 单个 [s, e]
            if isinstance(time_stamps, list) and time_stamps and isinstance(time_stamps[0], list):
                # 多时间片
                # 【修复】原来只有 "zh"/"yue"/"ja"，韩语 "ko" 被遗漏，
                # 导致 Qwen3-ASR 逐字符时间戳无法分配到每个音节块。
                if int_lang in ("zh", "yue", "ja", "ko") and len(text) > 1 and len(time_stamps) == len(text):
                    dur_each = sum((e - s) for s, e in time_stamps if s is not None and e is not None) / max(len(text), 1)
                    for i, ch in enumerate(text):
                        if i < len(time_stamps):
                            s, e = time_stamps[i]
                            if s is not None and e is not None and not _is_cjk_punct(ch):
                                entries.append((float(s), float(e), ch))
                else:
                    for item in time_stamps:
                        if isinstance(item, list) and len(item) >= 2:
                            s, e = item[0], item[1]
                            if s is not None and e is not None and not _is_cjk_punct(t):
                                entries.append((float(s), float(e), text))
            elif isinstance(time_stamps, list) and len(time_stamps) >= 2 and isinstance(time_stamps[0], (int, float)):
                s, e = time_stamps[0], time_stamps[1]
                if s is not None and e is not None and not _is_cjk_punct(t):
                    entries.append((float(s), float(e), text))
            elif isinstance(time_stamps, list) and time_stamps and isinstance(time_stamps[0], dict):
                for item in time_stamps:
                    s = item.get("start")
                    e = item.get("end")
                    t = (item.get("text") or "").strip()
                    if s is not None and e is not None and t and not _is_cjk_punct(t):
                        entries.append((float(s), float(e), t))

        return entries

    def align(self, audio_path: str, text: Optional[str], language: str,
              english_word_align: bool = False) -> Dict:
        t0 = time.time()
        try:
            int_lang = _normalize_lang(language)
            asr_lang = {
                "zh": "Chinese",
                "yue": "Cantonese",
                "en": "English",
                "ja": "Japanese",
                "ko": "Korean",
            }.get(int_lang, language)

            logger.info(f"[Qwen3-ASR] 调用独立服务: {self.endpoint}")
            result = self._call_qwen3_service(
                audio_path=audio_path,
                language=asr_lang,
                context="",
            )

            transcribed = (result.get("raw_text") or "").strip()
            segments = result.get("segments") or []

            entries = self._flatten_segments_to_entries(segments, int_lang)

            logger.info(f"[Qwen3-ASR] 转录文本: {transcribed[:120]}")

            if not entries and not transcribed:
                return {
                    "success": False,
                    "error": "Qwen3-ASR 无转录结果",
                    "processing_time": int((time.time() - t0) * 1000),
                }

            # 如果独立服务只返回文本，没有时间戳，则退化为均分
            if not entries and transcribed:
                total_s = self._get_audio_duration_100ns(audio_path) / 1e7
                # 【修复】原来只有 "zh"/"yue"/"ja" 走逐字符均分，"ko" 走
                # split()（按空格切词组），导致均分的每一格是整个 eojeol
                # 而非单个音节块。
                units = list(transcribed) if int_lang in ("zh", "yue", "ja", "ko") else transcribed.split()
                units = [u for u in units if u.strip()]
                if units:
                    dur = total_s / max(len(units), 1)
                    entries = [
                        (i * dur, (i + 1) * dur, u)
                        for i, u in enumerate(units)
                        if not _is_cjk_punct(u)
                    ]

            if not entries:
                return {
                    "success": False,
                    "error": "Qwen3-ASR 无时间戳输出",
                    "processing_time": int((time.time() - t0) * 1000),
                }

            final_text = transcribed or (text.strip() if text else "")
            if not transcribed and text:
                logger.warning("[Qwen3-ASR] 未返回转录文本，回退使用外部 text 进行后处理")

            # 【全局事后偏移校正 —— ASR 侧】
            # Qwen3-ASR 内部使用与独立 Qwen3-ForcedAligner 相同的对齐器
            # (qwen3_server.py 在 from_pretrained 时传入 forced_aligner=
            # FORCED_ALIGNER_ID)，因此输出的音节起始时间同样存在系统性偏早
            # 约 60 ms 的现象。这里复用 _apply_qwen3_fa_onset_delay()，
            # 传入 _for_asr=True 让它实时读取独立的
            # qwen3_asr_onset_delay_sec / qwen3_asr_min_syl_dur_sec 设置项
            # （可在设置页面单独调节，不影响 ForcedAligner 侧），把每个 entry
            # 的起始时间统一向后推，时长保持不变。安全约束（不重叠、不为负、
            # 时长兜底）同 ForcedAligner 侧，详见 _apply_qwen3_fa_onset_delay() 注释。
            entries = _apply_qwen3_fa_onset_delay(entries, _for_asr=True)

            # 【修复说明】Qwen3-ASR 不为标点输出时间戳，句末/句中停顿只能
            # 体现为相邻字符之间天然的时间间隙。之前这里传 fill_silences=False，
            # 导致这些天然间隙跟 WhisperX 旧版本一样只是数值上的空隙，没有
            # 写入 SIL 标记，SVP 工程生成阶段识别不到，音符仍然连在一起。
            # 改为 True 后交给 _fill_silences_lab() 按 ≥50ms 间隙自动补 SIL，
            # 这也是本模块文档注释里一直描述的预期行为。
            lab = self._word_entries_to_lab(
                entries,
                final_text,
                language,
                fill_silences=True,
                english_word_align=english_word_align,
            )

            return {
                "success": True,
                "lab_content": lab,
                "raw_text": final_text,
                "phoneme_text": transcribed,
                "audio_duration": self._get_audio_duration_100ns(audio_path),
                "processing_time": int((time.time() - t0) * 1000),
                "backend": "qwen3_asr_http",
            }

        except Exception as e:
            logger.error(f"[Qwen3-ASR] 失败: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "processing_time": int((time.time() - t0) * 1000),
            }

# ═════════════════════════════════════════════════════════════════════════════
# 6b. Qwen3-ForcedAligner 全局事后偏移校正
#
#     背景：实测 Qwen3-ForcedAligner 给出的音节起始时间系统性早于
#     WhisperX（同一段音频、118 个音节逐一比对，中位数偏移约 -61ms，
#     标准差 34ms），而音节时长本身基本一致（中位数偏移 ≈ 0），说明
#     这是模型对齐时把音节边界整体判定得偏早，不是速度/时长上的差异。
#
#     这里做的是「全局」校正：把每个音节的起始时间统一向后推
#     QWEN3_FA_ONSET_DELAY_SEC 秒，时长不变（即结束时间也同步向后推，
#     保持原有 duration）。效果上等价于把上一段的可用时长还给上一段，
#     当前段整体右移。
#
#     安全约束：
#       1) 不会把当前段的起点推到超过自己的终点（避免零长度/负长度段）。
#       2) 不会把当前段的起点推到与上一段终点重叠（避免吞掉上一段）。
#       3) 第一个音节起点不会被推到负数。
# ═════════════════════════════════════════════════════════════════════════════

# 全局偏移量（秒）。基于实测中位数 -0.061s 取整，便于后续按实际素材微调。
# 【可调设置】兜底默认值，实际生效值优先从设置页面读取，见 _get_alignment_tuning()。
QWEN3_FA_ONSET_DELAY_SEC: float = 0.06

# Qwen3-ASR 端：与 ForcedAligner 相同的系统性起始偏早问题。
# ASR 的时间戳由 Qwen3-ForcedAligner-0.6B 内嵌对齐器产生（qwen3_server.py
# 在加载 Qwen3ASRModel 时统一启用了 forced_aligner=FORCED_ALIGNER_ID），
# 因此表现出与独立 ForcedAligner 几乎一致的 "onset 偏早" 特性。
# 这里单独提取为可独立调节的常量，与 FA 常量保持相同初始值；
# 若后续实测 ASR 侧偏移量与 FA 侧有明显差异，可在不影响 FA 的前提下单独修改。
# 【可调设置】兜底默认值，实际生效值优先从设置页面读取，见 _get_alignment_tuning()。
QWEN3_ASR_ONSET_DELAY_SEC: float = 0.06

# 校正后允许的最短音节时长（秒），避免极端情况下时长被压成 0 或负数。
# 【可调设置】兜底默认值。FA / ASR 两侧现在各自独立可调（设置页面里的
# qwen3_fa_min_syl_dur_sec / qwen3_asr_min_syl_dur_sec），此前两侧共用
# 同一个 _QWEN3_FA_MIN_SYL_DUR_SEC 常量，这里拆分成两个同名兜底值，
# 默认取值仍然相同，行为与拆分前完全一致。
_QWEN3_FA_MIN_SYL_DUR_SEC: float = 0.02
_QWEN3_ASR_MIN_SYL_DUR_SEC: float = 0.02


def _apply_qwen3_fa_onset_delay(
    word_entries: List[Tuple[float, float, str]],
    delay_sec: Optional[float] = None,
    min_syl_dur_sec: Optional[float] = None,
    *,
    _for_asr: bool = False,
) -> List[Tuple[float, float, str]]:
    """
    对 Qwen3-ForcedAligner（或复用同一对齐器的 Qwen3-ASR）的 word_entries
    做全局事后偏移校正：把每个条目的起始时间整体向后推 delay_sec 秒，
    时长保持不变。

    Parameters
    ----------
    word_entries : [(start_sec, end_sec, token), ...]
        必须已按时间升序排列（Qwen3-FA 原始输出本身是按顺序给出的）。
    delay_sec : 向后推的秒数。正值 = 起始时间变晚（修正"偏早"问题）。
        未显式传入（None）时，从设置页面实时读取
        qwen3_asr_onset_delay_sec（_for_asr=True）或
        qwen3_fa_onset_delay_sec（_for_asr=False，默认），兜底 0.06s。
    min_syl_dur_sec : 校正后允许的最短音节时长（秒）。同样未传入时实时读取
        对应的 qwen3_asr_min_syl_dur_sec / qwen3_fa_min_syl_dur_sec 设置。
    _for_asr : 内部参数，标记调用方是 Qwen3-ASR 路径（影响上面两个参数的
        默认取值来源），调用方无需关心，_apply_qwen3_fa_onset_delay 的两处
        调用点已各自正确传入。

    Returns
    -------
    校正后的 word_entries，结构不变，可直接传给 _word_entries_to_lab()。
    """
    if delay_sec is None or min_syl_dur_sec is None:
        _tuning = _get_alignment_tuning()
        if delay_sec is None:
            delay_sec = _tuning["qwen3_asr_onset_delay_sec"] if _for_asr else _tuning["qwen3_fa_onset_delay_sec"]
        if min_syl_dur_sec is None:
            min_syl_dur_sec = _tuning["qwen3_asr_min_syl_dur_sec"] if _for_asr else _tuning["qwen3_fa_min_syl_dur_sec"]

    if not word_entries or delay_sec == 0:
        return word_entries

    corrected: List[Tuple[float, float, str]] = []
    prev_end = 0.0

    for i, (s, e, tok) in enumerate(word_entries):
        dur = max(0.0, float(e) - float(s))
        new_start = float(s) + delay_sec
        new_end = new_start + dur

        # 约束 1：不能早于上一段（校正后的）终点，避免吞掉上一段
        if new_start < prev_end:
            new_start = prev_end
            new_end = new_start + dur

        # 约束 2：起点不能为负
        if new_start < 0:
            shift = -new_start
            new_start = 0.0
            new_end += shift

        # 约束 3：时长兜底，避免压成 0 或负数（极少数边界情况下才会触发）
        if new_end - new_start < min_syl_dur_sec:
            new_end = new_start + min_syl_dur_sec

        corrected.append((new_start, new_end, tok))
        prev_end = new_end

    return corrected


# ═════════════════════════════════════════════════════════════════════════════
# 6c. Qwen3-ForcedAligner 长音频分段处理
#
#     问题背景：Qwen3-ForcedAligner-0.6B 是基于 Transformer 的强制对齐模型，
#     训练时见过的音频长度通常远短于一整首歌（几分钟）。把长音频整体一次性
#     喂给模型对齐时，时间戳会随着音频推进逐渐失准，实测在几分钟量级的
#     音频上会出现音节整体错位、越往后偏差越大的现象（用户反馈：QWEN3-FA
#     处理 5 分钟音频导致音标错位）。
#
#     解决思路：参考 WhisperXAligner.align() 已经验证过的"隔离对齐"思路
#     （见该函数顶部说明：逐句裁剪音频 → 在极短时序空间内单独对齐 → 全局
#     偏移拼回），把长音频切成若干个足够短的物理片段，分别调用模型对齐，
#     再把每段的局部时间戳加上该段起始时间偏移，拼接成完整的时间轴。
#
#     【v2 修正】旧版实现分两步独立决策，彼此互不知情：
#       1. 音频切点：用短时能量曲线找"能量最低"的位置（_plan_audio_chunks）。
#       2. 文本切点：按各段时长占比、用最大余数法把参考文本按字符数比例
#          分配（_split_text_by_duration_quota）。
#     这两步各自为政——音频切在能量最低处，文本切在字符比例算出来的位置，
#     二者对不上时，就会把同一个句子从中间硬生生切开，分别喂给两次独立的
#     模型调用。被腰斩的那半句在各自的短音频片段里既没有完整语义、也没有
#     完整韵律上下文，模型对齐出来的时间戳会系统性跑偏，表现正是用户反馈
#     的"每隔 ~20 秒的分段处音标错位"——本质上是"按固定时长硬切"的效果，
#     而不是真正按句子停顿切分。
#
#     新策略反过来做：先按标点把参考文本切成完整的句子，永远不在句子内部
#     下刀；再用句子的可发音字符数占比，粗略估算每个句子在全曲时间轴上的
#     大致位置，把连续整句分组到目标长度附近；最后只在"句子与句子之间"
#     这个天然停顿点上，用短时能量曲线在估算位置附近的一个小窗口内寻找
#     真实的安静区间，去微调物理切点该落在哪一帧——这一步只影响"喂给模型
#     的音频从哪里剪断"，参考文本的句子边界本身不会再被这一步改变。
#     由 _plan_sentence_aligned_chunks() 实现。
#
#     【v3 修正：彻底放弃按音频总时长的阈值/目标分段】旧版还留着一条"整段
#     没有任何句末标点时，退回 _plan_audio_chunks() + _split_text_by_
#     duration_quota() 按能量/字符比例强行切分"的兜底路径，并且是否启用
#     分段这件事本身也由 qwen3_fa_chunk_threshold_sec（音频超过多少秒才
#     分段）和 qwen3_fa_chunk_target_sec（每段目标多长）这两个纯时长维度
#     的阈值决定。这两个参数的共同问题是：它们都只看音频总时长，不看文本
#     本身的句子结构，本质上仍然是"猜"——猜语速均匀、猜句子大致落在哪个
#     时间点。猜得不准，切点就可能落在句子中间。
#
#     现在完全改为只依据参考文本自身的句末标点（。！？.!?…；\n，详见
#     _SENTENCE_END_RE）来决定切不切、切在哪——每个完整句子默认就是独立
#     的一段，不再需要"音频够长才分段"这个前置条件，也不再需要一个目标
#     时长去把多句"凑"成一段。_plan_audio_chunks() 与
#     _split_text_by_duration_quota() 这两个纯时长/比例切分函数仍保留在
#     文件里（未被删除，避免影响其他可能的调用方/后续复用），但
#     _plan_sentence_aligned_chunks() 不再调用它们：如果参考文本连一个
#     句末标点、逗号、顿号都没有（无法定位任何真实的句子边界），现在会
#     直接退回"整段单次对齐"，而不是按时长强行切分——宁可不切，也不在
#     猜出来的位置切错。
#
#     min_sentence_chunk_sec / max_sentence_chunk_sec 这两个参数仍然保留
#     且可在设置页面调整（app_settings.py 对应键
#     qwen3_fa_min_sentence_chunk_sec / qwen3_fa_max_sentence_chunk_sec），
#     但它们只处理"单句过短需要并入相邻句子" / "单句过长需要先按句内
#     逗号顿号再切一刀"这两种边缘情况，不影响"按句子边界切分"这个大前提。
# ═════════════════════════════════════════════════════════════════════════════

def _compute_rms_curve(
    audio,
    sr: int,
    frame_sec: float = 0.04,
    hop_sec: float = 0.02,
) -> Tuple["object", float]:
    """
    计算整段音频的短时 RMS 能量曲线，用于长音频分段切点搜索。

    与 _refine_sil_boundaries_by_energy() 里逐句、逐帧 Python 循环的实现
    不同——这里处理的是整首歌（可能几分钟，几万帧），用卷积一次性向量化
    算完，避免 Python 循环带来的明显延迟。

    Returns
    -------
    (rms, hop_sec)：rms 是 numpy 数组（float64），下标 i 对应时间
    i * hop_sec 秒；hop_sec 原样返回，方便调用方把下标换算回秒。
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


def _plan_audio_chunks(
    total_sec: float,
    rms,
    hop_sec: float,
    target_chunk_sec: float,
    rel_threshold: float = 0.06,
    abs_floor: float = 0.0008,
    abs_ceiling: float = 0.003,
    min_pause_sec: float = 0.12,
) -> List[Tuple[float, float]]:
    """
    根据目标分段长度和能量曲线，把 [0, total_sec] 切成若干段
    [(start_sec, end_sec), ...]，切点优先落在"真实停顿"（持续足够长的
    安静区间，典型是句末换气）上；即便完全找不到这样的安静区，也会保证
    任何一段都不超过一个硬上限（target_chunk_sec 的 1.5 倍），不会退化回
    "整段不切"。

    【历史问题】旧实现只在搜索窗口内取能量最低的单帧作为切点。这在语速
    较快、辅音密集的中文人声念白里并不可靠——爆破音/塞音的短暂闭塞
    （通常只有 1～2 个能量采样点）本身能量也很低，会被误判成"停顿"，
    于是切点经常落在句子中间的辅音闭塞处，而不是句末真正的换气停顿，
    观感上表现为"看起来像是按固定时长硬切"，而不是按句末静音切分。

    【现改法】不再看单帧最小值，而是用与 _refine_sil_boundaries_by_energy()
    一致的自适应阈值（本曲 70 分位能量 × rel_threshold，并钳制到
    [abs_floor, abs_ceiling]）先把搜索窗口内的帧二值化为"安静/不安静"，
    再找出所有连续安静的区间（run），只保留时长 >= min_pause_sec（默认
    120ms，明显长于普通塞音闭塞、但短于绝大多数句末换气）的候选，从中
    选取中心点离"理想切分位置"（cur + target_chunk_sec）最近的一段，取
    其中点作为切分点。找不到任何满足最短时长的安静区间时（比如整段语速
    极快、几乎没有停顿），才退回旧逻辑：直接取窗口内能量最低的单帧，
    保证算法永远不会因为找不到"完美"停顿而放弃分段。

    Parameters
    ----------
    total_sec : 音频总时长（秒）
    rms       : _compute_rms_curve() 得到的能量曲线
    hop_sec   : 能量曲线每个采样点对应的时间步长（秒）
    target_chunk_sec : 目标分段长度（秒）。实际每段长度落在
        [target_chunk_sec * 0.5, target_chunk_sec * 1.5] 区间内。
    rel_threshold, abs_floor, abs_ceiling : 安静阈值 = clip(rel_threshold ×
        全曲 70 分位能量, abs_floor, abs_ceiling)，含义与
        _refine_sil_boundaries_by_energy() 中同名参数一致，取值也沿用
        该函数已验证过的默认值，保持全文件"何为安静"判定标准统一。
    min_pause_sec : 判定为"真实停顿"所需的最短持续安静时长（秒）。低于
        此时长的安静区间（通常是辅音闭塞）不会被当作候选切点。

    Returns
    -------
    [(start_sec, end_sec), ...]，按时间顺序首尾相接，覆盖整个
    [0, total_sec] 区间；total_sec <= 0 时返回单个覆盖全部（可能为空）
    区间的列表。
    """
    import numpy as np

    if total_sec <= 0 or target_chunk_sec <= 0:
        return [(0.0, max(total_sec, 0.0))]

    max_chunk_sec = target_chunk_sec * 1.5
    min_search_sec = target_chunk_sec * 0.5
    rms = np.asarray(rms, dtype=np.float64)
    n_frames = len(rms)

    # 与 _refine_sil_boundaries_by_energy() 同一套自适应阈值公式：用整曲
    # 70 分位能量代表"正常发声电平"，阈值取其一个比例并钳制到合理的绝对
    # 区间，避免整段偏响/偏轻的素材导致阈值失真。
    voiced_level = float(np.percentile(rms, 70)) if n_frames else 0.0
    silence_threshold = max(abs_floor, min(rel_threshold * voiced_level, abs_ceiling))
    min_pause_frames = max(1, int(round(min_pause_sec / hop_sec)))

    def _find_silence_split(search_start: float, search_end: float, ideal_t: float) -> Optional[float]:
        """在 [search_start, search_end] 内找时长 >= min_pause_frames 的
        连续安静区间，返回离 ideal_t 最近的一段的中点；找不到则返回 None。
        """
        start_idx = max(0, min(int(search_start / hop_sec), n_frames - 1))
        end_idx = max(start_idx + 1, min(int(search_end / hop_sec), n_frames))
        window = rms[start_idx:end_idx]
        if window.size == 0:
            return None

        is_quiet = window < silence_threshold
        best_center_t: Optional[float] = None
        best_dist = None
        run_start = None
        for i in range(len(is_quiet) + 1):
            quiet_here = bool(is_quiet[i]) if i < len(is_quiet) else False
            if quiet_here and run_start is None:
                run_start = i
            elif not quiet_here and run_start is not None:
                run_len = i - run_start
                if run_len >= min_pause_frames:
                    center_idx = start_idx + (run_start + i) / 2.0
                    center_t = center_idx * hop_sec
                    dist = abs(center_t - ideal_t)
                    if best_dist is None or dist < best_dist:
                        best_dist = dist
                        best_center_t = center_t
                run_start = None
        return best_center_t

    boundaries: List[float] = [0.0]
    cur = 0.0
    # 安全阀，避免异常数据（如 hop_sec 极小）导致循环次数失控。
    max_iterations = int(total_sec / max(min_search_sec, 1e-3)) + 4
    iterations = 0

    while total_sec - cur > max_chunk_sec and iterations < max_iterations:
        iterations += 1
        search_start = cur + min_search_sec
        search_end = min(total_sec, cur + max_chunk_sec)
        ideal_t = cur + target_chunk_sec

        split_t = None
        if search_end > search_start and n_frames > 0:
            split_t = _find_silence_split(search_start, search_end, ideal_t)

        if split_t is None:
            # 回退：窗口内没有任何满足最短时长的真实安静区（比如整段
            # 语速很快、几乎不停顿）。退回旧逻辑——直接取窗口内能量最低
            # 的单帧，保证任何情况下都不会放弃分段。
            if search_end <= search_start or n_frames == 0:
                split_t = cur + target_chunk_sec
            else:
                start_idx = max(0, min(int(search_start / hop_sec), n_frames - 1))
                end_idx = max(start_idx + 1, min(int(search_end / hop_sec), n_frames))
                window = rms[start_idx:end_idx]
                rel_idx = int(np.argmin(window))
                split_t = (start_idx + rel_idx) * hop_sec

        # 防御：确保切点严格前进，避免浮点/取整问题导致原地踏步死循环。
        split_t = max(split_t, cur + min_search_sec * 0.5)
        split_t = min(split_t, total_sec)
        boundaries.append(split_t)
        cur = split_t

    boundaries.append(total_sec)

    # 合并过短的收尾分段（< 目标长度的 25%）到前一段，避免出现几秒钟的
    # 畸零小段——对齐模型在极短音频上，误差占比会明显被放大。
    while len(boundaries) >= 3 and (boundaries[-1] - boundaries[-2]) < target_chunk_sec * 0.25:
        boundaries.pop(-2)

    spans = list(zip(boundaries[:-1], boundaries[1:]))
    return [(s, e) for s, e in spans if e > s] or [(0.0, total_sec)]


# ── 句末标点 / 句内软停顿标点：用于按句切分参考文本 ─────────────────────────
# 与 WhisperXAligner.align() 里 `_re.split(r'[。！？；\n…!?]+', ...)` 使用
# 同一套句末标点定义，保持全文件"何为一句话"标准统一；这里用捕获组
# 保留标点本身（归属前一句），而不是像 WhisperX 那样直接丢弃。
_SENTENCE_END_RE = _re.compile(r'([。！？；\n…!?]+)')
# 句内软停顿（逗号/顿号）：只在单个句子本身长到超过硬上限时，才退一步
# 用它在句子内部再切一刀（仍然是标点位置，不是任意字符位置）。
_SOFT_PAUSE_RE = _re.compile(r'([，、,]+)')


def _split_text_by_delims(text: str, delim_re: "_re.Pattern") -> List[str]:
    """
    通用"保留分隔符"切分：delim_re 匹配到的分隔符始终归属于它前面那一段，
    返回的所有子串按顺序拼接后与原始 text 完全一致（不丢一个字符，含
    标点和空白）；结尾没有分隔符收尾的剩余文本并入最后一个单元，不会
    单独产生一个"裸文本"分段。

    text 为空返回 []；没有匹配到任何分隔符返回 [text]。
    """
    if not text:
        return []
    parts = delim_re.split(text)
    units: List[str] = []
    buf = ""
    for i, p in enumerate(parts):
        if p is None:
            continue
        if i % 2 == 1:
            buf += p
            units.append(buf)
            buf = ""
        else:
            buf += p
    if buf:
        if units:
            units[-1] += buf
        else:
            units.append(buf)
    return [u for u in units if u]


def _count_spoken_units(text_unit: str, int_lang: str) -> int:
    """
    统计一段文本里的"可发音单元"个数，计数标准与 _split_text_by_duration_quota()
    保持一致：中/粤/日/韩按字符计（连续拉丁字母合并成一个单元），其他
    语言按空白分隔的词计。用于按字符数比例粗略估算某句话在全曲时间轴上
    的位置。
    """
    if not text_unit:
        return 0
    if int_lang in ("zh", "yue", "ja", "ko"):
        units = _re.findall(r"[A-Za-z']+|.", text_unit, flags=_re.S)
        return sum(1 for u in units if not _is_cjk_punct(u) and not u.isspace())
    return sum(1 for u in text_unit.split() if u.strip())


def _compute_silence_threshold(
    rms,
    rel_threshold: float = 0.06,
    abs_floor: float = 0.0008,
    abs_ceiling: float = 0.003,
) -> float:
    """
    与 _plan_audio_chunks() 内部使用的自适应安静阈值公式完全一致（本曲
    70 分位能量 × rel_threshold，钳制到 [abs_floor, abs_ceiling]），抽出
    为独立函数供 _plan_sentence_aligned_chunks() 复用，确保全文件"何为
    安静"判定标准统一，不会出现两套切点用两个不同阈值的情况。
    """
    import numpy as np

    rms = np.asarray(rms, dtype=np.float64)
    n = len(rms)
    voiced_level = float(np.percentile(rms, 70)) if n else 0.0
    return max(abs_floor, min(rel_threshold * voiced_level, abs_ceiling))


def _find_quiet_run_center(
    rms,
    hop_sec: float,
    search_start: float,
    search_end: float,
    ideal_t: float,
    silence_threshold: float,
    min_pause_frames: int,
    prefer_longest: bool = False,
) -> Optional[float]:
    """
    在 [search_start, search_end] 内寻找时长 >= min_pause_frames 的连续
    安静区间，返回其中一段的中点秒数；找不到满足条件的安静区间则返回 None。

    prefer_longest=False（默认）：返回离 ideal_t 最近的一段——适合窗口
    较窄、ideal_t 本身已经比较可信的场景。
    prefer_longest=True：优先返回时长最长的一段（时长相同再比离 ideal_t
    的远近）——适合窗口较宽、ideal_t 本身可能有较大误差（如歌唱场景，
    音符时值/旋律拖长会让"按字符数比例估算时间"明显失准）的场景：真正
    的换气停顿通常比偶发的辅音闭塞/字间空隙明显更长，"最长" 比"离估算
    位置最近"更能定位到真实停顿。

    从 _plan_audio_chunks() 内部的同名逻辑抽出为独立函数，供
    _plan_sentence_aligned_chunks() 在句子边界附近微调物理切点时复用。
    """
    import numpy as np

    rms = np.asarray(rms, dtype=np.float64)
    n_frames = len(rms)
    if n_frames == 0:
        return None

    start_idx = max(0, min(int(search_start / hop_sec), n_frames - 1))
    end_idx = max(start_idx + 1, min(int(search_end / hop_sec), n_frames))
    window = rms[start_idx:end_idx]
    if window.size == 0:
        return None

    is_quiet = window < silence_threshold
    best_center_t: Optional[float] = None
    best_score: Optional[Tuple[float, float]] = None   # (排序主键, 排序次键)
    run_start: Optional[int] = None
    for i in range(len(is_quiet) + 1):
        quiet_here = bool(is_quiet[i]) if i < len(is_quiet) else False
        if quiet_here and run_start is None:
            run_start = i
        elif not quiet_here and run_start is not None:
            run_len = i - run_start
            if run_len >= min_pause_frames:
                center_idx = start_idx + (run_start + i) / 2.0
                center_t = center_idx * hop_sec
                dist = abs(center_t - ideal_t)
                score = (float(run_len), -dist) if prefer_longest else (-dist, float(run_len))
                if best_score is None or score > best_score:
                    best_score = score
                    best_center_t = center_t
            run_start = None
    return best_center_t


def _find_quietest_point(
    rms, hop_sec: float, search_start: float, search_end: float,
) -> Optional[float]:
    """
    在 [search_start, search_end] 内直接返回能量最低的单帧对应时间——
    不要求形成"连续安静区间"，用作找不到任何合格安静区间时的最终兜底，
    保证任何情况下都能给出一个比"完全不看音频、直接用估算位置"更接近
    真实停顿的物理切点。
    """
    import numpy as np

    rms = np.asarray(rms, dtype=np.float64)
    n_frames = len(rms)
    if n_frames == 0:
        return None
    start_idx = max(0, min(int(search_start / hop_sec), n_frames - 1))
    end_idx = max(start_idx + 1, min(int(search_end / hop_sec), n_frames))
    window = rms[start_idx:end_idx]
    if window.size == 0:
        return None
    rel_idx = int(np.argmin(window))
    return (start_idx + rel_idx) * hop_sec


def _plan_sentence_aligned_chunks(
    total_sec: float,
    rms,
    hop_sec: float,
    text: str,
    int_lang: str,
    min_chunk_sec: float = 3.0,
    max_chunk_sec: float = 20.0,
) -> Tuple[List[Tuple[float, float]], List[str]]:
    """
    按句子边界规划长音频分段（替代"先切音频、再按比例切文本"的旧方案）。

    步骤：
      1. 按句末标点（_SENTENCE_END_RE）把参考文本切成完整的句子，标点
         归属前一句，切分结果拼接后与原文本完全一致。
      2. 若某个句子自身的估算时长就已经超过 max_chunk_sec，退一步用句内
         逗号/顿号（_SOFT_PAUSE_RE）再切一刀——仍然是标点位置，不会切在
         任意字符中间。
      3. 用各句可发音字符数占比，粗略估算每句结束时刻在全曲时间轴上的
         位置（假设语速大致均匀，只用来定位"句子边界"落在物理音频的
         大致哪个位置，不用于决定切不切、也不直接拿去切文本）。
      4. 分组：默认每一句（可能已按软停顿拆分过）就是独立的一段——真正
         做到"每个完整句子交给 QWEN3-FA 独立处理一次"；只有当累计时长
         还没达到 min_chunk_sec 这个最短门槛时，才继续并入下一句，避免
         生成过短、难以稳定对齐的独立音频片段（比如"嗯。""啊！"这类
         单字/叹词句）。
      5. 只在组与组的分界点（两个完整句子之间的天然停顿）上，用短时能量
         曲线在估算位置附近的小窗口内搜索真实安静区间，微调物理切点；
         找不到明显安静区间时退回估算位置本身。

    退化路径：如果参考文本完全没有可用的标点（连句末标点、逗号、顿号都
    没有，无法定位任何真实的句子/短语边界——比如整段是一句话到底的口水
    文本），现在直接退回"整段单次对齐"（不切分），而不是按音频时长/字符
    比例强行切分：宁可不切，也不在没有真实边界依据的位置猜切点。

    Returns
    -------
    (spans, chunk_texts)：spans 为 [(start_sec, end_sec), ...]，按时间顺序
    首尾相接覆盖 [0, total_sec]；chunk_texts 与 spans 等长，每个元素都是
    原始 text 的一段连续子串（含标点/空白），按顺序拼接后与原始 text
    完全相同。
    """
    sentences = _split_text_by_delims(text, _SENTENCE_END_RE)
    if len(sentences) <= 1:
        # 整段没有句末标点（比如通篇到最后才有一个句号，或完全没有），
        # 句末标点这一级切不出多句——退一步整体按逗号/顿号切分，仍然是
        # 标点位置，只是粒度更细。只有连逗号都没有时，才真正放弃"按标点
        # 切分"，回退到纯音频能量分段。
        soft_sentences = _split_text_by_delims(text, _SOFT_PAUSE_RE)
        if len(soft_sentences) > 1:
            logger.info(
                "[Qwen3-FA] 参考文本未检测到句末标点，退一步按逗号/顿号切分"
                f"（{len(soft_sentences)} 段）"
            )
            sentences = soft_sentences
        else:
            logger.warning(
                "[Qwen3-FA] 参考文本未检测到任何可用的标点（句末/逗号/顿号），"
                "无法定位任何真实的句子边界——已放弃按音频时长/字符比例强行"
                "切分的旧兜底方案（那样切点不落在任何真实边界上，反而更容易"
                "切在句子中间），退回整段单次对齐"
            )
            return [(0.0, total_sec)], [text]

    # 单句本身过长（估算时长超过 max_chunk_sec）时，先用句内软停顿标点
    # 再切一刀，避免它被硬塞进一个远超预期的独立片段。
    probe_counts = [_count_spoken_units(s, int_lang) for s in sentences]
    total_probe = sum(probe_counts) or 1
    expanded: List[str] = []
    for s, c in zip(sentences, probe_counts):
        est_dur = total_sec * c / total_probe
        if est_dur > max_chunk_sec:
            sub_units = _split_text_by_delims(s, _SOFT_PAUSE_RE)
            if len(sub_units) > 1:
                expanded.extend(sub_units)
                continue
        expanded.append(s)
    sentences = expanded

    counts = [_count_spoken_units(s, int_lang) for s in sentences]
    total_units = sum(counts) or 1
    cum = 0
    est_boundaries: List[float] = []
    for c in counts:
        cum += c
        est_boundaries.append(total_sec * cum / total_units)
    if est_boundaries:
        est_boundaries[-1] = total_sec

    # ── 分组：默认每句独立成组，过短的句子才与下一句合并 ─────────────────
    # 与旧版"累积到 target_chunk_sec 才切一刀"的区别：这里判定条件反过来
    # 用一个很小的 min_chunk_sec 下限——绝大多数句子的估算时长都会立刻
    # 超过这个下限，所以几乎每句话都会立刻单独成组；只有明显偏短的句子
    # （比如感叹词、单字回应）才会继续并入后面的句子，避免出现几百毫秒
    # 的畸零独立片段。
    n = len(sentences)
    groups: List[Tuple[int, int]] = []   # [(start_idx, end_idx_exclusive), ...]
    start_idx = 0
    group_start_t = 0.0
    for i in range(n):
        cur_dur = est_boundaries[i] - group_start_t
        if cur_dur >= min_chunk_sec or i == n - 1:
            groups.append((start_idx, i + 1))
            group_start_t = est_boundaries[i]
            start_idx = i + 1

    # 合并过短的收尾分组，阈值同样用 min_chunk_sec 本身（不再是某个大
    # target 的固定比例）。
    while len(groups) >= 2:
        s0, e0 = groups[-1]
        prev_end_t = est_boundaries[s0 - 1] if s0 > 0 else 0.0
        dur_last = est_boundaries[e0 - 1] - prev_end_t
        if dur_last < min_chunk_sec:
            s_prev, _e_prev = groups[-2]
            groups[-2] = (s_prev, e0)
            groups.pop()
        else:
            break

    if len(groups) <= 1:
        return [(0.0, total_sec)], ["".join(sentences)]

    # ── 只在"组与组之间"（两个完整句子之间的天然停顿）微调物理切点 ──────
    # 三级回退搜索，而不是原来的单一窄窗口 + 离估算位置最近：
    #
    #   【背景】实测发现，即使 target/threshold 两个设置项本身已经正确
    #   生效，长音频（尤其是歌唱/演唱场景，也包括语速拖长、大量拖腔的
    #   Talkloid 素材）里仍会出现切点落空、导致相邻两段各自吃掉对方半个
    #   字/音节的错位。
    #
    #   【第一次修复的遗留问题】上一版把窗口半径改成了
    #   max(0.6, min(6.0, neighbor_gap * 0.7))——虽然把绝对上限从 3.0s
    #   放宽到了 6.0s，但下限只有 0.6s，一旦 neighbor_gap（当前句子与
    #   相邻句子的估算时长）本身就很小，radius 会直接被压到 0.6s 的
    #   地板值。用真实歌曲音频实测验证过：两处实际报错的边界
    #   （132.3s 应为 133.5~134.2s 之间的真实换气段；255.0s 应为
    #   255.8~256.5s 之间的真实换气段）之所以没找到正确的安静区间，
    #   都是因为算出来的 radius 刚好卡在 0.6s 上下，宽窗口的边缘只
    #   差 0.2~1s 就能盖住真正的停顿，结果因为差这一点点全军覆没，
    #   要么退回估算位置本身，要么误抓了估算位置附近一个明显更短、
    #   但恰好够着 250ms 门槛的假停顿（辅音间隙）。
    #
    #   【这次的修复】把窗口半径的下限从 0.6s 大幅提高到 2.5s（上限
    #   从 6.0s 提高到 8.0s）——理由：character-count 比例估算在歌唱
    #   场景下的系统性误差，实测经常达到 1~2 秒量级，0.6s 的地板值
    #   在数学上就不可能覆盖这个误差范围，无论"最短停顿判定"或
    #   "优先最长"这些策略层面的改进多么精细都无济于事，必须先保证
    #   窗口本身够大。2.5s 的地板值以本曲验证过的两个失败案例为下限
    #   （分别需要约 1.55s / 1.15s 的偏移量）并留出安全余量。
    #
    #   同时把原来"宽窗口找不到就切换到一个更窄的窗口"的第二级回退，
    #   改成"复用同一个宽窗口、只放宽停顿门槛"——窄窗口在这次实测中
    #   被验证并不能提供任何额外价值：真正有用的从来是"窗口是否够大
    #   到能覆盖真实停顿"，而不是"要不要缩小窗口"。缩小窗口只会让
    #   本来就该被第一级宽窗口覆盖到的正确停顿，在第二级里重新变得
    #   够不着。
    #
    #   第一级（严格 + 宽窗口）：最短停顿判定 250ms（明显长于辅音闭塞，
    #     更接近真实换气/乐句间隔时长），prefer_longest=True——宁可要
    #     "这个宽窗口里最长的一段安静"，也不要"离一个可能不准的估算
    #     位置最近的一小段安静"。
    #   第二级（宽松，复用同一宽窗口）：第一级没有任何一段安静区间
    #     达到 250ms 时，把门槛放宽到 120ms，改为离 ideal_t 最近优先——
    #     用于这段真的没有明显长停顿、只有短促字间空隙的场景。
    #   第三级（兜底）：前两级都没有搜到任何"连续安静区间"时，退而
    #     求其次，在同一个宽窗口内直接取能量最低的单帧——即使算不上
    #     一段持续静音，大概率也比"完全不看音频、盲切在估算位置"更
    #     接近真实停顿。
    #   只有宽窗口本身退化为空（比如两个估算边界几乎重合的极端情况）时，
    #     才真正用 ideal_t 兜底。
    silence_threshold = _compute_silence_threshold(rms)
    strong_min_pause_frames = max(1, int(round(0.25 / hop_sec)))
    loose_min_pause_frames = max(1, int(round(0.12 / hop_sec)))

    cut_times: List[float] = [0.0]
    for gi in range(len(groups) - 1):
        _, end_idx = groups[gi]
        boundary_idx = end_idx - 1
        ideal_t = est_boundaries[boundary_idx]

        prev_t = est_boundaries[boundary_idx - 1] if boundary_idx > 0 else 0.0
        next_t = (
            est_boundaries[boundary_idx + 1]
            if boundary_idx + 1 < len(est_boundaries)
            else total_sec
        )
        neighbor_gap = min(ideal_t - prev_t, next_t - ideal_t)

        # 窗口半径：下限从 0.6s 提高到 2.5s，上限从 6.0s 提高到 8.0s
        # （详见上方注释里两个实测失败案例的具体数字）。
        wide_radius = max(2.5, min(8.0, neighbor_gap))
        wide_start = max(cut_times[-1], ideal_t - wide_radius)
        wide_end = min(total_sec, ideal_t + wide_radius)

        split_t = None
        if wide_end > wide_start:
            split_t = _find_quiet_run_center(
                rms, hop_sec, wide_start, wide_end, ideal_t,
                silence_threshold, strong_min_pause_frames,
                prefer_longest=True,
            )
        if split_t is None and wide_end > wide_start:
            split_t = _find_quiet_run_center(
                rms, hop_sec, wide_start, wide_end, ideal_t,
                silence_threshold, loose_min_pause_frames,
                prefer_longest=False,
            )
        if split_t is None and wide_end > wide_start:
            split_t = _find_quietest_point(rms, hop_sec, wide_start, wide_end)
        if split_t is None:
            # 窗口本身退化为空，才真正使用估算位置兜底——句子边界本身
            # 通常就是天然的换气点。
            split_t = ideal_t

        split_t = max(split_t, cut_times[-1] + 0.05)
        split_t = min(split_t, total_sec)
        cut_times.append(split_t)

    cut_times.append(total_sec)
    spans = list(zip(cut_times[:-1], cut_times[1:]))
    chunk_texts = ["".join(sentences[s:e]) for s, e in groups]
    return spans, chunk_texts


def _merge_short_spans(
    spans: List[Tuple[float, float]],
    texts: List[str],
    min_chunk_sec: float,
) -> Tuple[List[Tuple[float, float]], List[str]]:
    """
    合并时长仍然低于 min_chunk_sec 的分段（常见于整个 WhisperX ASR 段
    恰好就是一个感叹词/单字回应）到相邻分段：优先并入前一段（若存在），
    否则并入后一段；文本按时间顺序拼接，物理区间取并集。

    只做区间合并 + 文本拼接，不重新对齐、不检查合并结果是否跨越脚本
    切换硬边界——这一层处理的是"WhisperX ASR 段"级别的收尾合并，更细
    粒度的脚本切换/句内软停顿边界已经在每个 ASR 段内部调用
    _plan_sentence_aligned_chunks() 时处理过；一个独立 ASR 段恰好短于
    min_chunk_sec 且恰好在脚本切换点上，这个组合本身已经是双重边缘
    情况，概率极低，此处不再额外处理。

    Parameters
    ----------
    spans, texts : 长度相同、按时间顺序排列，texts[i] 对应 spans[i]。

    Returns
    -------
    合并后的 (spans, texts)，长度可能小于输入；只要输入非空，输出也
    非空（最少剩 1 个分段）。
    """
    if len(spans) <= 1:
        return spans, texts

    merged_spans = [list(s) for s in spans]
    merged_texts = list(texts)

    i = 0
    while i < len(merged_spans) and len(merged_spans) > 1:
        dur = merged_spans[i][1] - merged_spans[i][0]
        if dur >= min_chunk_sec:
            i += 1
            continue
        if i > 0:
            # 并入前一段：前一段的结束时刻推到当前段结束，文本追加在后面。
            merged_spans[i - 1][1] = merged_spans[i][1]
            merged_texts[i - 1] = merged_texts[i - 1] + merged_texts[i]
            merged_spans.pop(i)
            merged_texts.pop(i)
            # 不前进 i：原来的第 i+1 个分段现在下标变为 i，需要重新
            # 检查它自己是否也过短（多个连续过短分段会被逐个滚入同一个
            # 不断增长的前一段，直至累计够长或列表耗尽）。
        else:
            # 没有前一段（i == 0）：并入后一段，反向操作。
            merged_spans[i + 1][0] = merged_spans[i][0]
            merged_texts[i + 1] = merged_texts[i] + merged_texts[i + 1]
            merged_spans.pop(i)
            merged_texts.pop(i)
            # i 仍为 0，重新检查合并后的新第 0 段。

    return [tuple(s) for s in merged_spans], merged_texts


def _stitch_spans_to_full_coverage(
    spans: List[Tuple[float, float]],
    rms,
    hop_sec: float,
    total_sec: float,
) -> List[Tuple[float, float]]:
    """
    WhisperX 自己的 ASR 段之间通常会留有真实的停顿间隙（VAD 判定为静音，
    不计入任何一段的 start/end），必须把这些间隙"分配"给相邻两段中的
    一段，才能保证返回的 spans 无缝覆盖 [0, total_sec]——_align_chunked()
    是按 spans 逐段裁剪音频喂给 Qwen3-FA 的，如果分段之间留有空隙，
    空隙对应的那截音频会被完全跳过、永远不会被对齐或出现在最终结果里。

    间隙本身几乎总是真实静音（正是 WhisperX 的 VAD 把它判定为"没有
    语音"才没有归入任何一段），所以直接在这个间隙区间内用短时能量
    曲线找一个安静点作为两段共同的新边界即可；找不到明显安静区间时
    退回间隙中点兜底。开头（第一段之前）和结尾（最后一段之后）的
    间隙没有"两侧都是真实分段"这个前提，直接扩展到 0 / total_sec。

    传入的 spans 必须已按时间顺序排列；返回一份新的、彼此首尾相接、
    完整覆盖 [0, total_sec] 的分段列表（不改变分段数量，只调整边界）。
    """
    if not spans:
        return spans

    stitched = [list(s) for s in spans]
    silence_threshold = _compute_silence_threshold(rms)
    # 间隙场景下不需要很长的静音判定（间隙本身通常就是真实停顿），
    # 30ms 足够滤掉数值噪声，同时不会因为门槛过高而找不到候选。
    min_pause_frames = max(1, int(round(0.03 / hop_sec)))

    if stitched[0][0] > 0.0:
        stitched[0][0] = 0.0
    if stitched[-1][1] < total_sec:
        stitched[-1][1] = total_sec

    for i in range(len(stitched) - 1):
        gap_start = stitched[i][1]
        gap_end = stitched[i + 1][0]
        if gap_end <= gap_start:
            # 相邻两段恰好相接、或（理论上）互相重叠：直接取中点拆开，
            # 保证不产生负长度分段。
            mid = (gap_start + gap_end) / 2.0
            stitched[i][1] = mid
            stitched[i + 1][0] = mid
            continue

        ideal_t = (gap_start + gap_end) / 2.0
        cut = _find_quiet_run_center(
            rms, hop_sec, gap_start, gap_end, ideal_t,
            silence_threshold, min_pause_frames, prefer_longest=True,
        )
        if cut is None:
            cut = _find_quietest_point(rms, hop_sec, gap_start, gap_end)
        if cut is None:
            cut = ideal_t
        stitched[i][1] = cut
        stitched[i + 1][0] = cut

    return [tuple(s) for s in stitched]


def _plan_chunks_via_whisperx_rough_pass(
    audio_path: str,
    text: str,
    int_lang: str,
    total_sec: float,
    rms,
    hop_sec: float,
    min_chunk_sec: float,
    max_chunk_sec: float,
    whisper_model: str = "large-v3",
    device: str = "auto",
) -> Tuple[Optional[List[Tuple[float, float]]], Optional[List[str]]]:
    """
    用 WhisperX 的"粗测"ASR 转录结果规划 Qwen3-FA 长音频分段边界，替代
    _plan_sentence_aligned_chunks() 里"假设语速均匀、按参考文本字符数
    占比反推每句在全曲时间轴上的位置"这一纯估算方案。

    【动机】_plan_sentence_aligned_chunks() 的边界估算本质上仍是"猜"：
    只知道全曲总时长和每句的字符数占比，不知道音频里真实的语音在哪。
    演唱/拖腔/语速不均的素材上，这个估算的系统性误差可达 1~2 秒——
    对切分后再各自独立跑一次 Qwen3-FA 的架构来说，误差直接体现为
    "喂给某一段的物理音频，开头/结尾多带了或少带了半句话"，Qwen3-FA
    自己在这一段内部的对齐再精细也无法纠正一个从一开始就错的物理
    边界，这正是长音频对齐日志里大量"自愈修复/均匀分配"退化兜底的
    根本原因，而不是 Qwen3-FA 本身对齐能力的问题。

    【思路】WhisperX 是真正跑一遍语音识别，Whisper 自身的 VAD 分段
    天然落在真实语音的起止点上（不依赖字符比例假设）。这里只用它的
    ASR 转录步骤（_transcribe_rough_segments，不做后续 wav2vec2 强制
    对齐，更快）拿到这些真实分段时间戳，再用 _bind_ref_text_by_asr_count()
    把原始参考文本（保留标点，不用 WhisperX 自己识别出的文字）按各段
    自己识别出的字数配额切给对应分段——分段的物理边界来自真实 ASR，
    分段的对齐文本仍然是用户提供的原始参考文本，两者结合但互不污染。

    每个 WhisperX 分段内部仍可能包含多句参考文本、或时长仍然超过
    max_chunk_sec；这里递归复用 _plan_sentence_aligned_chunks() 本身
    对每个分段单独再做一次精细规划——区别在于这次喂给它的 total_sec
    是这一个 WhisperX 段自己的真实时长（通常几秒到二十秒量级），而不
    是整曲总时长，字符比例估算的误差范围从"整首歌"缩小到"这一个
    真实分段"，句子切分/脚本切换硬边界/句内软停顿再切一刀/组间能量
    精修等全部逻辑原样复用，不需要重新实现。跨 WhisperX 段之间可能
    残留的过短分段，最后用 _merge_short_spans() 再合并一次。

    Returns
    -------
    (spans, chunk_texts)：格式、约定与 _plan_sentence_aligned_chunks()
    完全一致，可直接替换其调用处。任何一步失败（WhisperX 未安装/加载
    失败、ASR 无输出、参考文本或识别内容为空导致无法按字数配额绑定等）
    都返回 (None, None)，调用方应无缝回退到 _plan_sentence_aligned_
    chunks()，不让整个对齐任务失败——这条路径纯粹是"锦上添花"，任何
    环节出问题都不应该影响任务本身能否成功。
    """
    try:
        wx_ok, wx_msg = WhisperXAligner.check_available()
        if not wx_ok:
            logger.warning(
                f"[Qwen3-FA][WhisperX 粗测] WhisperX 不可用（{wx_msg}），"
                "回退到按参考文本字符比例估算的分段方案"
            )
            return None, None

        whisperx_aligner = get_aligner("whisperx", device=device, whisper_model=whisper_model)
        rough = whisperx_aligner._transcribe_rough_segments(audio_path, int_lang)
        if not rough.get("success"):
            logger.warning(
                f"[Qwen3-FA][WhisperX 粗测] ASR 转录失败（{rough.get('error')}），"
                "回退到按参考文本字符比例估算的分段方案"
            )
            return None, None

        raw_segments = sorted(
            rough["raw_segments"], key=lambda s: float(s.get("start", 0.0))
        )

        # 按各段自身识别出的字数为配额，把原始参考文本（保留标点）顺序
        # 切给对应段——只借用 WhisperX 的时间戳和字数，不使用它识别出
        # 的文字内容本身（原地修改 raw_segments[i]["text"]，此后就是
        # 原始参考文本的切片，不再是 WhisperX 自己的识别结果）。
        bound_ok = _bind_ref_text_by_asr_count(text, raw_segments, int_lang)
        if not bound_ok:
            logger.warning(
                "[Qwen3-FA][WhisperX 粗测] 参考文本或 ASR 识别内容为空，"
                "无法按字数配额绑定，回退到按参考文本字符比例估算的分段方案"
            )
            return None, None

        spans: List[Tuple[float, float]] = []
        chunk_texts: List[str] = []
        n_frames = len(rms)

        for seg in raw_segments:
            seg_start = max(0.0, float(seg.get("start", 0.0)))
            seg_end = min(total_sec, float(seg.get("end", seg_start)))
            seg_text = (seg.get("text") or "").strip()
            if seg_end <= seg_start or not seg_text:
                continue

            seg_dur = seg_end - seg_start
            start_idx = max(0, min(int(round(seg_start / hop_sec)), n_frames))
            end_idx = max(start_idx + 1, min(int(round(seg_end / hop_sec)), n_frames))
            local_rms = rms[start_idx:end_idx] if n_frames else rms

            local_spans, local_texts = _plan_sentence_aligned_chunks(
                seg_dur, local_rms, hop_sec, seg_text, int_lang,
                min_chunk_sec, max_chunk_sec,
            )
            for (ls, le), lt in zip(local_spans, local_texts):
                spans.append((seg_start + ls, seg_start + le))
                chunk_texts.append(lt)

        if not spans:
            logger.warning(
                "[Qwen3-FA][WhisperX 粗测] 所有 ASR 段绑定参考文本后均为空，"
                "回退到按参考文本字符比例估算的分段方案"
            )
            return None, None

        # WhisperX 的 ASR 段之间通常留有真实停顿间隙（VAD 判定的静音，
        # 不计入任何一段）；必须先把这些间隙缝合掉，spans 才能像
        # _plan_sentence_aligned_chunks() 的输出一样无缝覆盖 [0, total_sec]
        # ——否则间隙对应的音频会被 _align_chunked() 完全跳过。
        spans = _stitch_spans_to_full_coverage(spans, rms, hop_sec, total_sec)

        spans, chunk_texts = _merge_short_spans(spans, chunk_texts, min_chunk_sec)

        logger.info(
            f"[Qwen3-FA][WhisperX 粗测] 基于 {len(raw_segments)} 个 WhisperX "
            f"ASR 段（真实语音边界）规划出 {len(spans)} 个分段，"
            "不再依赖字符比例估算"
        )
        return spans, chunk_texts

    except Exception as e:
        logger.warning(
            f"[Qwen3-FA][WhisperX 粗测] 分段规划异常（{e}），"
            "回退到按参考文本字符比例估算的分段方案",
            exc_info=True,
        )
        return None, None


def _split_text_by_duration_quota(
    text: str,
    chunk_durations: List[float],
    int_lang: str,
) -> List[str]:
    """
    把完整参考文本（保留原始标点/空白）按各分段时长比例，依次切给对应
    分段，供长音频分段对齐时每段独立喂给 Qwen3-ForcedAligner。

    做法与 _bind_ref_text_by_asr_count() 一致（最大余数法，把各段配额
    总和精确钳制为参考文本总的可发音单元数），区别在于这里没有 ASR
    识别结果可作为更精确的配额来源（Qwen3-FA 是纯强制对齐器，不会先跑
    一遍 ASR），只能退而求其次按分段时长占比分配——精度不如"按 ASR
    识别字数分配"，但仍然远好于完全不切分。

    可发音单元定义：
      - 中/粤/日/韩 → 逐字符（与 Qwen3-FA 输出粒度一致）
      - 其他语言（如英语）→ 逐个空白分隔的词

    Returns
    -------
    与 chunk_durations 等长的字符串列表，各元素首尾已 strip()。
    调用方拿到的这些子串按顺序拼接（去掉 strip 产生的空白差异后）
    等价于原始 text，因此后续把所有分段的对齐结果合并后，仍可以用
    完整的原始 text 一次性调用 _word_entries_to_lab()，字符数校验
    不会因为分段而失败。
    """
    n = len(chunk_durations)
    if not text:
        return ["" for _ in range(n)]
    if n <= 1:
        return [text]

    is_cjk_char_lang = int_lang in ("zh", "yue", "ja", "ko")

    if is_cjk_char_lang:
        # 逐字符切分，但把连续的拉丁字母/撇号（歌词中夹杂的英文单词，如
        # "Vocaloid"）合并成一个整体单元，避免被从中间切开分到两个不同
        # 分段——那样会导致该单词在两段各自的参考文本里都只剩半个词，
        # 降低这半个词所在片段的对齐质量（虽然最终仍会被
        # _merge_latin_letter_chars 之类的下游逻辑正确处理，但源头上能
        # 避免更好）。
        units = _re.findall(r"[A-Za-z']+|.", text, flags=_re.S)

        def _is_spoken(u: str) -> bool:
            return not _is_cjk_punct(u) and not u.isspace()
    else:
        # \S+ / \s+ 交替切分：spoken 单元是"词"，空白原样保留、附着在
        # 前一个词所在的分段（不单独占配额，也不会被丢弃）。
        units = _re.findall(r"\S+|\s+", text)

        def _is_spoken(u: str) -> bool:
            return not u.isspace()

    spoken_total = sum(1 for u in units if _is_spoken(u))
    if spoken_total == 0:
        result = ["" for _ in range(n)]
        result[0] = text
        return result

    total_dur = sum(chunk_durations) or 1.0
    raw_quota = [spoken_total * (d / total_dur) for d in chunk_durations]
    int_quota = [int(q) for q in raw_quota]
    remainder = spoken_total - sum(int_quota)
    if remainder > 0:
        order = sorted(
            range(len(raw_quota)), key=lambda i: raw_quota[i] - int_quota[i], reverse=True
        )
        for i in order[:remainder]:
            int_quota[i] += 1

    # 确保每个分段至少分到 1 个可发音单元（前提是总单元数够分），避免
    # 出现完全空文本的分段导致该段对齐直接失败。
    if spoken_total >= n:
        for i, q in enumerate(int_quota):
            if q > 0:
                continue
            donor = max(range(n), key=lambda k: int_quota[k])
            if int_quota[donor] <= 1:
                break
            int_quota[donor] -= 1
            int_quota[i] = 1

    cum_quota: List[int] = []
    acc = 0
    for q in int_quota:
        acc += q
        cum_quota.append(acc)

    chunks_text = ["" for _ in range(n)]
    spoken_seen = 0
    idx = 0
    for u in units:
        if _is_spoken(u):
            spoken_seen += 1
            while idx < len(cum_quota) - 1 and spoken_seen > cum_quota[idx]:
                idx += 1
        chunks_text[idx] += u

    return [c.strip() for c in chunks_text]


# ═════════════════════════════════════════════════════════════════════════════
# 7. Qwen3ForcedAligner
# ═════════════════════════════════════════════════════════════════════════════

class Qwen3ForcedAligner(AltAlignerBase):
    DEFAULT_MODEL = "Qwen/Qwen3-ForcedAligner-0.6B"

    def __init__(self, *args, device="cpu", batch_size: int = 8, **kwargs):
        super().__init__(*args, **kwargs)

        self._device = device
        self._model = None
        # Qwen3-ForcedAligner 官方接口没有真正意义上可调的批大小（每次
        # align() 调用都是单条音频单次前向），这里保留该参数纯粹是为了
        # 与 Qwen3ASRAligner / NeMoForcedAligner 构造签名一致、以及在 OOM
        # 自动降级重试时的日志里作为参考值展示，不影响任何实际推理行为，
        # 详见 _align_single_chunk() 里的说明。
        self.batch_size = max(1, int(batch_size))
        # 记录当前 self._model 实际加载在哪个设备上（"cuda" / "cpu"），
        # 与用户请求的 self._device 分开记录——命中显存不足或 CUDA 不可用
        # 时会把这个值改成 "cpu"，但不会改动 self._device 本身（用户下次
        # 新建任务时的初始尝试仍然遵循原始设置，不永久性地"记仇"）。
        self._loaded_device: Optional[str] = None
        # 本实例（单例，跨任务复用）是否已经因为显存不足/CUDA 不可用而
        # 永久性回退到 CPU——一旦发生，同一批 get_aligner() 缓存生命周期
        # 内后续所有分段/任务直接跳过 GPU 尝试，避免每次都重新触发一次
        # 必然失败的 OOM 再重试，浪费时间。
        self._cpu_fallback_sticky: bool = False

        # ✅ 补上这一行（关键修复）
        self.model_id = kwargs.get("model_id", self.DEFAULT_MODEL)

    @staticmethod
    def check_available() -> Tuple[bool, str]:
        try:
            import qwen_asr  # noqa: F401
            return True, "qwen-asr 已就绪"
        except ImportError as e:
            return False, f"未安装 qwen-asr: pip install -U qwen-asr ({e})"

    def _load_model(self, force_cpu: bool = False):
        """
        懒加载 Qwen3-ForcedAligner 模型。

        force_cpu=True 用于显存不足 / CUDA 不可用时的自动降级路径
        （见 _align_single_chunk 里的 OOM 重试逻辑）：无论 self._device
        原始请求的是什么，都强制在 CPU 上重新加载一份模型，并把结果记入
        self._cpu_fallback_sticky，避免同一实例在后续调用里反复尝试 GPU
        再失败。已经加载好、且加载时的设备与本次请求一致时直接复用，
        不重复加载。
        """
        target_device = "cpu" if (force_cpu or self._cpu_fallback_sticky) else _safe_device(
            getattr(self, "_device", "cpu")
        )

        if self._model is not None and self._loaded_device == target_device:
            return

        import torch
        from qwen_asr import Qwen3ForcedAligner as Qwen3FA

        dtype = torch.bfloat16 if target_device.startswith("cuda") else torch.float32

        if self._model is not None and self._loaded_device != target_device:
            logger.info(
                f"[Qwen3-FA] 运行设备变化: {self._loaded_device} → {target_device}，重新加载模型..."
            )
            self._model = None
            try:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

        logger.info(f"[Qwen3-FA] 加载模型 device={target_device} dtype={dtype}")
        try:
            self._model = Qwen3FA.from_pretrained(
                self.model_id,
                dtype=dtype,
                device_map=target_device,
            )
        except Exception as e:
            # 模型加载阶段本身就可能因为显存不足 / CUDA Toolkit 缺失或
            # 版本不匹配而失败（不是所有 CUDA 环境问题都要等到真正推理
            # 那一刻才暴露）——这里同样识别到就直接改在 CPU 上重新加载，
            # 而不是把这类环境报错原样抛给用户。target_device 已经是
            # "cpu" 时说明 CPU 本身都加载失败，属于更严重的问题（例如
            # 模型文件损坏/依赖缺失），不再重试，原样抛出。
            if target_device == "cpu" or not _is_cuda_oom_or_env_error(e):
                raise
            logger.warning(
                f"[Qwen3-FA] 在 {target_device} 上加载模型失败（{e}），"
                "自动切换到 CPU 重新加载..."
            )
            try:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            target_device = "cpu"
            dtype = torch.float32
            self._model = Qwen3FA.from_pretrained(
                self.model_id,
                dtype=dtype,
                device_map=target_device,
            )
        self._loaded_device = target_device
        if force_cpu or target_device == "cpu":
            self._cpu_fallback_sticky = True

    def align(self, audio_path: str, text: Optional[str], language: str,
              english_word_align: bool = False) -> Dict:
        t0 = time.time()
        if not text:
            return {
                "success": False,
                "error": "Qwen3-ForcedAligner 需要参考文本（text 不能为空）",
                "processing_time": 0,
            }

        try:
            self._load_model()

            lang_name = _to_qwen_lang_name(language)
            if not lang_name:
                return {
                    "success": False,
                    "error": f"Qwen3-ForcedAligner 不支持语言: {language}",
                    "processing_time": int((time.time() - t0) * 1000),
                }

            int_lang = _normalize_lang(language)
            total_sec = self._get_audio_duration_100ns(audio_path) / 1e7

            tuning = _get_alignment_tuning()

            # 【总开关】qwen3_fa_enable_sentence_chunking（默认 False/
            # 禁用）：为 False 时完全跳过下面 _align_chunked() 的整套
            # "按句末标点规划分段 + 逐段独立对齐"流程，直接整段单次对齐，
            # 行为等同于分段逻辑引入之前的原始版本——不做静音感知分段，
            # 也不会用到 WhisperX 粗测预处理（该子开关此时已被
            # app_settings.save_settings() 强制置为 False）。
            if not _get_sentence_chunking_enabled():
                logger.info(
                    f"[Qwen3-FA] 音频时长 {total_sec:.1f}s，"
                    "「按句子分段对齐」总开关已禁用，直接整段单次对齐"
                )
                word_entries = self._align_single_chunk(audio_path, text, lang_name, int_lang)
            else:
                # 【v3：不再用音频总时长的阈值/目标时长来决定要不要分段】
                # 旧版 qwen3_fa_chunk_threshold_sec（超过多少秒才分段）/
                # qwen3_fa_chunk_target_sec（每段目标多长）都已放弃使用——
                # 这两个参数只看音频总时长，不看文本本身的句子结构，本质
                # 上仍是"猜"语速、猜切点。现在统一交给 _align_chunked ->
                # _plan_sentence_aligned_chunks 按参考文本自身的句末标点
                # （。！？.!?…；\n）决定切不切、切在哪：只要文本能按标点
                # 切出 2 句及以上，就会分段独立对齐；如果只有 1 句（或短到
                # 需要与相邻句合并成 1 组）、或完全没有可用标点，
                # _align_chunked 内部会自动退化为整段单次对齐——因此这里
                # 不需要、也不应该再用一个单独的时长阈值去"提前"决定是否
                # 进入分段路径。
                # min/max 这两个值只处理"单句过短需要并入相邻句子"（min）/
                # "单句过长需要先按句内逗号顿号再切一刀"（max）这两种边缘
                # 情况，不影响"按句子边界切分"这个大前提本身。
                min_sentence_chunk_sec = float(tuning.get("qwen3_fa_min_sentence_chunk_sec", 3.0))
                max_sentence_chunk_sec = float(tuning.get("qwen3_fa_max_sentence_chunk_sec", 20.0))

                logger.info(
                    f"[Qwen3-FA] 音频时长 {total_sec:.1f}s，按参考文本句末标点"
                    f"规划分段（单句下限 {min_sentence_chunk_sec:.1f}s / 上限 "
                    f"{max_sentence_chunk_sec:.1f}s；仅 1 句或无可用标点时会"
                    "自动退化为整段单次对齐）"
                )
                word_entries = self._align_chunked(
                    audio_path, text, lang_name, int_lang, total_sec,
                    min_sentence_chunk_sec, max_sentence_chunk_sec,
                )

            if not word_entries:
                return {
                    "success": False,
                    "error": "Qwen3-ForcedAligner 无对齐输出",
                    "processing_time": int((time.time() - t0) * 1000),
                }

            # 【全局事后偏移校正】实测 Qwen3-FA 的音节起始时间系统性早于
            # WhisperX（中位数约 -61ms），时长本身基本一致，说明是边界
            # 判定整体偏早而非速度差异。这里把每个音节起点统一向后推
            # QWEN3_FA_ONSET_DELAY_SEC 秒（时长不变），让视觉/听感上的
            # 音节位置更接近 WhisperX。详见 _apply_qwen3_fa_onset_delay()。
            # 分段场景下这里在合并后的全局 entries 上统一做一次即可
            # （而不是每段各做一次）：约束 1（不早于上一段终点）在段与段
            # 的拼接处同样成立，效果与整段单次对齐时完全一致。
            word_entries = _apply_qwen3_fa_onset_delay(word_entries)

            # 同上：Qwen3-ForcedAligner 同样不输出标点的时间戳，停顿只能
            # 体现为真实词与词之间的时间间隙，需要 fill_silences=True
            # 才能把这些间隙转换成 SVP 能识别的 SIL 标记。
            lab = self._word_entries_to_lab(
                word_entries, text, language, fill_silences=True,
                english_word_align=english_word_align,
            )

            return {
                "success": True,
                "lab_content": lab,
                "raw_text": text,
                "phoneme_text": text,
                "audio_duration": self._get_audio_duration_100ns(audio_path),
                "processing_time": int((time.time() - t0) * 1000),
                "backend": "qwen3_aligner",
            }

        except Exception as e:
            logger.error(f"[Qwen3-FA] 失败: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "processing_time": int((time.time() - t0) * 1000),
            }

    # ============================================================
    # Qwen3-FA 局部对齐结果的"退化区间"检测与自愈修复
    # ------------------------------------------------------------
    # 背景（详见 _align_chunked 里 QWEN3_FA_CHUNK_FALLBACK 分支的说明）：
    # Qwen3 自己的音频编码器在处理较长音频时，内部是按固定窗口分块处理
    # 的，不具备静音感知能力。这意味着即使外层已经按句子边界 + 静音做过
    # 一次切分，只要单个分段本身仍不够短（或者音频本身信息密度高、语速
    # 快、中英混杂），依然可能在分段内部撞上 Qwen3 内部窗口的边界，产生
    # 两种典型的退化时间戳：
    #
    #   (a) "压缩坍缩"：连续一串字全部被挤进远小于正常语速所需的时间
    #       窗口（比如 20 个字被压进 0.4 秒），识别出的文字内容本身是对
    #       的，只是时间戳错了。
    #   (b) "伪等距停顿"（实测最常见于长音频单次整段对齐的尾部）：几乎
    #       每一个字后面都被插入一段几十到一百多毫秒、彼此时长异常接近
    #       的"假 SIL"，形成一种和真实语句停顿完全无关、却机械地非常
    #       规律的"字-停-字-停"节拍。
    #
    # 这两种情况的根源都不是"分段切点选得不对"，而是模型自身在这段局部
    # 音频上的时间戳判定本身就不可靠——所以修复思路不是去猜一个"安全"的
    # chunk_target_sec（换一个更长/更难的音频随时可能又不安全），而是在
    # 拿到每一段的对齐结果后自动检测是否退化；退化了就把这一小段音频再
    # 切细、递归重新对齐，直到恢复正常或者短到没有再切的意义为止。
    # ============================================================
    _DEGEN_MIN_RUN = 4                # 至少连续多少个非 SIL token 才构成"一段"
    _DEGEN_ABS_DUR_FLOOR = 0.045      # 压缩坍缩：单字时长下限（秒），低于视为异常
    _DEGEN_METRONOME_MIN_PAIRS = 3    # 伪等距停顿：至少多少个连续"字+紧跟停顿"才算异常
                                       # （实测：5 会漏掉退化区间里不够"整齐"的前段，3 覆盖更完整）
    _DEGEN_METRONOME_CV_MAX = 0.30    # 伪等距停顿：字时长变异系数上限（越小越"整齐划一"）
    _DEGEN_GAP_MIN = 0.03             # 伪等距停顿：判定为"插入了一次停顿"的最小间隙（秒）
                                       # 低于此值视为正常连读之间的自然过渡，不计入
    _DEGEN_MIN_REPAIR_SPAN_SEC = 0.6  # 退化区间物理时长低于此值时，直接均匀分配，不再递归
    _DEGEN_MAX_RECURSION_DEPTH = 3    # 递归重新对齐的最大深度，避免极端情况下死循环

    @staticmethod
    def _find_degenerate_spans(
        entries: List[Tuple[float, float, str]],
    ) -> List[Tuple[int, int]]:
        """
        扫描单段局部对齐结果（entries 为该段自身时间轴、从 0 开始的
        [(start, end, token), ...]），返回退化区间列表，每项是
        (start_idx, end_idx) —— entries 里 [start_idx, end_idx) 这个左闭
        右开区间被判定为退化，需要重新处理。

        两种检测逻辑（细节见上方说明）：
          1. 压缩坍缩：连续 >= _DEGEN_MIN_RUN 个非 SIL token，每个时长都
             小于 _DEGEN_ABS_DUR_FLOOR。
          2. 伪等距停顿：连续 >= _DEGEN_METRONOME_MIN_PAIRS 个"字"，每个
             字后面都紧跟着一段不可忽略的时间间隙（>= _DEGEN_GAP_MIN），
             且"字时长+间隙"这个周期本身的变异系数很低（人类说话不可能
             每个字之间都精准地插入几乎等长的停顿）。

             注意：这里故意不要求"紧跟的是字面上的 SIL token"——
             _align_single_chunk() 内部调用本函数时，拿到的是 Qwen3
             模型的原始输出，这个阶段根本不会出现 "SIL" 这个 token
             （SIL 是后续 _word_entries_to_lab() 转换成 .lab 文本时才
             插入的）。如果只认字面 "SIL"，这条检测在实际线上跑的时候
             永远不会触发，只有拿最终生成的 .lab 文件离线测试时才会
             命中——这里改成直接看相邻两个 token 之间的时间间隙，不管
             这个间隙有没有被后续步骤物化成一个显式的 SIL 条目。
        """
        spans: List[Tuple[int, int]] = []
        n = len(entries)

        # ---- 检测 1：压缩坍缩 ----
        i = 0
        while i < n:
            s, e, tok = entries[i]
            if tok != "SIL" and (e - s) < Qwen3ForcedAligner._DEGEN_ABS_DUR_FLOOR:
                j = i
                while (
                    j < n
                    and entries[j][2] != "SIL"
                    and (entries[j][1] - entries[j][0]) < Qwen3ForcedAligner._DEGEN_ABS_DUR_FLOOR
                ):
                    j += 1
                if j - i >= Qwen3ForcedAligner._DEGEN_MIN_RUN:
                    spans.append((i, j))
                i = j
            else:
                i += 1

        # ---- 检测 2：伪等距停顿（字+隐式间隙的规律重复）----
        i = 0
        while i < n - 1:
            run_start = i
            periods: List[float] = []
            j = i
            while (
                j + 1 < n
                and entries[j][2] != "SIL"
                and entries[j + 1][2] != "SIL"
            ):
                gap = entries[j + 1][0] - entries[j][1]
                if gap < Qwen3ForcedAligner._DEGEN_GAP_MIN:
                    break  # 间隙太小，只是正常连读之间的过渡，不算"卡了一下"
                tok_dur = entries[j][1] - entries[j][0]
                periods.append(tok_dur + gap)
                j += 1
            run_len = j - run_start
            if run_len >= Qwen3ForcedAligner._DEGEN_METRONOME_MIN_PAIRS:
                mean_p = sum(periods) / len(periods)
                if mean_p > 0:
                    var = sum((p - mean_p) ** 2 for p in periods) / len(periods)
                    cv = (var ** 0.5) / mean_p
                    if cv < Qwen3ForcedAligner._DEGEN_METRONOME_CV_MAX:
                        spans.append((run_start, j + 1))
            i = j if j > i else i + 1

        if not spans:
            return []

        # 合并重叠/相邻区间
        spans.sort()
        merged: List[Tuple[int, int]] = [spans[0]]
        for s, e in spans[1:]:
            ls, le = merged[-1]
            if s <= le:
                merged[-1] = (ls, max(le, e))
            else:
                merged.append((s, e))
        return merged

    def _repair_degenerate_spans(
        self,
        entries: List[Tuple[float, float, str]],
        audio_path: str,
        lang_name: str,
        int_lang: str,
        chunk_dur_sec: Optional[float] = None,
        _depth: int = 0,
    ) -> List[Tuple[float, float, str]]:
        """
        对 _align_single_chunk() 刚拿到的局部结果做退化检测 + 自愈修复。

        对每个探测到的退化区间：
          1. 取区间前一个条目的结束时间 / 区间后一个条目的开始时间作为
             "已知可信"的时间边界 [t_lo, t_hi]（区间在片段开头/结尾时，
             分别退化为 0.0 / 该片段自身音频总时长）。
          2. 若 [t_lo, t_hi] 还有一定长度、且递归深度未超限：把这段音频
             物理裁出来，单独重新调用一次 _align_single_chunk()——一小
             段（通常几秒）音频撞上 Qwen3 内部窗口边界的概率远低于原来
             那一整段，多数情况下这一步就能拿到正常结果。
          3. 重新对齐后的结果会再跑一次同样的退化检测（递归），如果还
             是不行就继续切小，直到深度耗尽。
          4. 深度耗尽、区间物理时长太短、或找不到可发音字符时，退化为
             按字符数均匀分配（与 _align_chunked 里已有的整段失败兜底
             逻辑一致）。
        """
        spans = self._find_degenerate_spans(entries)
        if not spans:
            return entries

        try:
            import soundfile as sf
        except ImportError:
            logger.warning("[Qwen3-FA][自愈修复] 缺少 soundfile，无法裁剪音频做局部重对齐，跳过自愈")
            return entries

        try:
            audio, sr = sf.read(audio_path, always_2d=False)
        except Exception as e:
            logger.warning(f"[Qwen3-FA][自愈修复] 读取音频失败（{e}），跳过自愈")
            return entries
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)

        total_local_dur = chunk_dur_sec if chunk_dur_sec is not None else len(audio) / sr

        import tempfile
        import shutil
        import os as _os

        tmp_dir = tempfile.mkdtemp(prefix="qwen3_fa_selfheal_")
        try:
            new_entries = list(entries)
            # 从后往前处理，避免下标在拼接替换后错位
            for span_start, span_end in reversed(spans):
                t_lo = entries[span_start - 1][1] if span_start > 0 else 0.0
                t_hi = entries[span_end][0] if span_end < len(entries) else total_local_dur
                span_dur = t_hi - t_lo
                bad_tokens = [tok for _, _, tok in entries[span_start:span_end] if tok != "SIL"]

                repaired: Optional[List[Tuple[float, float, str]]] = None

                if (
                    span_dur >= Qwen3ForcedAligner._DEGEN_MIN_REPAIR_SPAN_SEC
                    and _depth < Qwen3ForcedAligner._DEGEN_MAX_RECURSION_DEPTH
                    and bad_tokens
                ):
                    sub_text = (
                        "".join(bad_tokens) if int_lang in ("zh", "yue", "ja", "ko")
                        else " ".join(bad_tokens)
                    )
                    st_samp = max(0, int(round(t_lo * sr)))
                    en_samp = min(len(audio), int(round(t_hi * sr)))
                    cropped = audio[st_samp:en_samp]
                    if len(cropped) >= int(0.2 * sr):
                        sub_wav = _os.path.join(tmp_dir, f"heal_{span_start}_{span_end}.wav")
                        try:
                            sf.write(sub_wav, cropped, sr)
                            sub_entries = self._align_single_chunk(
                                sub_wav, sub_text, lang_name, int_lang, _depth=_depth + 1,
                            )
                            if sub_entries and not self._find_degenerate_spans(sub_entries):
                                repaired = [(s + t_lo, e + t_lo, tok) for s, e, tok in sub_entries]
                                logger.info(
                                    f"[Qwen3-FA][自愈修复] 区间 [{t_lo:.2f}s–{t_hi:.2f}s] "
                                    f"重新对齐成功（depth={_depth + 1}）"
                                )
                            else:
                                logger.warning(
                                    f"[Qwen3-FA][自愈修复] 区间 [{t_lo:.2f}s–{t_hi:.2f}s] "
                                    f"重新对齐后仍异常，回退为均匀分配"
                                )
                        except Exception as exc:
                            logger.warning(
                                f"[Qwen3-FA][自愈修复] 区间 [{t_lo:.2f}s–{t_hi:.2f}s] "
                                f"重新对齐失败（{exc}），回退为均匀分配"
                            )

                if repaired is None:
                    units = (
                        [u for u in "".join(bad_tokens) if u.strip()]
                        if int_lang in ("zh", "yue", "ja", "ko")
                        else [u for u in bad_tokens if u.strip()]
                    )
                    if units:
                        dur = span_dur / len(units)
                        repaired = [
                            (t_lo + i * dur, t_lo + (i + 1) * dur, u)
                            for i, u in enumerate(units)
                        ]
                        logger.warning(
                            f"[Qwen3-FA][自愈修复] 区间 [{t_lo:.2f}s–{t_hi:.2f}s] "
                            f"改为按 {len(units)} 个可发音单元均匀分配（精度较低）"
                        )
                    else:
                        repaired = []

                new_entries[span_start:span_end] = repaired
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return new_entries

    def _align_single_chunk(
        self, audio_path: str, text: str, lang_name: str,
        int_lang: Optional[str] = None,
        _depth: int = 0,
    ) -> List[Tuple[float, float, str]]:
        """
        对单个音频片段（可以是完整音频，也可以是分段切出的短片段）调用
        Qwen3-FA 模型做一次强制对齐，返回该片段自身时间轴（从 0 开始）上
        的 [(start_sec, end_sec, token), ...]。

        分段场景下，调用方（_align_chunked）负责把这里返回的局部时间戳
        加上该段在整段音频里的起始偏移，换算成全局时间戳。

        int_lang 用于退化区间自愈修复（_repair_degenerate_spans）里判断
        "按字符切分还是按空格切分"；不传时退化为不做自愈（保持旧行为），
        调用方应尽量把已有的 int_lang 传进来。

        显存不足 / CUDA 环境异常自动降级：调用 self._model.align() 时如果
        命中 CUDA OOM（或底层抛出的错误信息表明是 CUDA Toolkit 未正确
        安装等环境问题），会调用 _load_model(force_cpu=True) 把模型整个
        重新加载到 CPU 上再重试一次；成功后 self._cpu_fallback_sticky
        会保持为 True，同一实例后续所有调用直接走 CPU，不再重复尝试 GPU。
        与 WhisperXAligner._transcribe_with_oom_retry() 的"腰斩 batch_size
        重试"不同——Qwen3-ForcedAligner 官方 API 本身没有 batch_size 这个
        参数可调（每次都是单条音频单次前向），能做的唯一有效降级就是
        整体换到 CPU；_get_qwen3_batch_size() 读到的设置值目前只作为
        日志里展示的参考值，不传给 self._model.align()。
        """
        self._load_model()
        try:
            results = self._model.align(
                audio=audio_path,
                text=text,
                language=lang_name,
            )
        except Exception as e:
            # 用关键词判断而不是只捕获 RuntimeError：不同 torch/transformers
            # 版本在显存不足或 CUDA 环境异常时抛出的异常类型不完全一致
            # （RuntimeError 最常见，但也可能是 AssertionError 等），
            # _is_cuda_oom_or_env_error() 内部只看错误信息关键词，能覆盖
            # 更多实际场景；非此类错误（如文本/音频本身有问题）原样抛出。
            if not _is_cuda_oom_or_env_error(e):
                raise
            logger.warning(
                f"[Qwen3-FA] GPU 推理失败（{e}），自动切换到 CPU 重新加载模型并重试"
                f"（参考批大小设置 qwen3_batch_size={_get_qwen3_batch_size()}，"
                "但 Qwen3-ForcedAligner 官方接口本身不支持按批降级，"
                "此处直接整体切换运行设备）..."
            )
            try:
                import torch as _torch_oom
                if _torch_oom.cuda.is_available():
                    _torch_oom.cuda.empty_cache()
            except Exception:
                pass
            self._load_model(force_cpu=True)
            results = self._model.align(
                audio=audio_path,
                text=text,
                language=lang_name,
            )

        # 官方示例里 results[0][0].text / start_time / end_time
        entries: List[Tuple[float, float, str]] = []
        for item in (results[0] if results else []):
            tok = (getattr(item, "text", "") or "").strip()
            if not tok or _is_cjk_punct(tok):
                continue
            entries.append((float(item.start_time), float(item.end_time), tok))

        if entries and int_lang is not None:
            entries = self._repair_degenerate_spans(
                entries, audio_path, lang_name, int_lang, _depth=_depth,
            )

        return entries

    # ============================================================
    # "尾部死区"检测与修复
    # ------------------------------------------------------------
    # 背景：_plan_sentence_aligned_chunks() 给每一段分配参考文本时，是
    # 按"整段音频总时长 × 累计字符数占比"这个粗略估算来切句子边界的；而
    # 真正物理切开音频的切点 split_t，是在这个估算点附近另外做静音搜索
    # 选出来的，两者并不保证严丝合缝。如果某一段实际语速比估算的平均语
    # 速慢（或者相邻两段语速差异较大），就会出现"这一段自己的音频其实
    # 比分配给它的参考文本要长"——多出来的那截真实语音，其实是下一句话
    # 提前说出来的开头部分，物理上被划进了当前段的音频范围。强制对齐
    # 只能对齐"给它的文本"，本段文本提前用完之后，自己音频里剩下的那
    # 一截真实语音就没有任何文本可对应，最终被后续 fill_silences 逻辑
    # 整段填成一个和真实停顿毫无关系、但看起来"合理"的巨大 SIL——播放
    # 时就是一段明显比其他停顿长得多、里面其实还有人声的"死区"。
    #
    # 【v2 修正：不再跨段借文本】旧版做法是把下一段参考文本开头一小截
    # "借"过来拼到本段文本末尾、用本段音频重新对齐一次，再尝试从下一段
    # 文本里裁掉"估计被借走的部分"。这个"估计"依赖用新旧对齐结果的条目
    # 数量差反推消费了多少个可发音单元，本质上是近似值——一旦数量估计
    # 有偏差，下一段文本就会被裁多或裁少一点：下一段自己的音频裁剪范围
    # 完全没有变（还是从旧的 split_t 开始），只是文本被裁短了，裁剪数量
    # 一旦对不上，下一段开头就会出现"文本比音频少一点/多一点"的局部
    # 错位——这正是用户反馈的"边界一旦互相借用，局部就会歪"的根源。
    #
    # 新做法只搬运时间边界，完全不碰任何一段的文本：本段的对齐结果（到
    # last_end 为止）已经是正确的，不需要重新对齐；那截"死区"里的真实
    # 语音，物理上大概率就是下一句话的开头——所以只需要把本段与下一段
    # 之间的物理切点，从原来的估算位置 seg_dur，收紧到 [last_end,
    # seg_dur] 区间内一个真正安静的位置（找不到就直接退到 last_end，把
    # 这一整截疑似人声原样让给下一段）。调用方 (_align_chunked) 据此把
    # 下一段的音频裁剪起点相应前移，下一段自己的参考文本完全不用动——
    # 它本来就是完整的下一句话，音频起点往前挪一点之后，反而能覆盖到
    # 它提前说出来的开头部分。
    # ============================================================

    # 尾部空白超过这个时长（相对该段自身音频总长）才需要怀疑是死区，而
    # 不是真实的长停顿（实测这批音频里真实停顿基本都在 1 秒以内）。
    _DEAD_ZONE_MIN_SEC = 1.2
    # 判断死区里是不是"真的有声音"用的 RMS 门限（输入是 float32 [-1,1]
    # 归一化后的采样点）；纯静音/底噪通常远低于这个值。
    _DEAD_ZONE_RMS_FLOOR = 0.01

    def _resolve_trailing_dead_zone_boundary(
        self,
        local_entries: List[Tuple[float, float, str]],
        cropped_audio,
        sr: int,
        start_sec: float,
        seg_dur: float,
        global_rms,
        hop_sec: float,
    ) -> Optional[float]:
        """
        检测"这一段末尾出现一大截本不该存在的空白，但那截音频里其实还有
        真实人声"的情况（细节见上方模块说明），如果确认存在，返回一个
        新的、应该收紧到的物理切点（本曲全局时间轴，单位秒）；调用方
        (_align_chunked) 用这个值前移下一段音频裁剪的起点即可，不需要
        对任何一段的参考文本做任何修改。

        Returns
        -------
        Optional[float]：全局时间轴上的新切点。None 表示未检测到需要
        处理的死区（尾部空白不够长，或那截空白确实是真安静）——此时
        调用方应保持原有物理切点不变。
        """
        if not local_entries:
            return None

        last_end_local = local_entries[-1][1]
        tail_gap = seg_dur - last_end_local
        if tail_gap < self._DEAD_ZONE_MIN_SEC:
            return None  # 尾部空白不够长，不值得怀疑

        # 用简单的 RMS 快速判断这截"疑似死区"是不是真的安静。
        st = max(0, int(round(last_end_local * sr)))
        en = min(len(cropped_audio), int(round(seg_dur * sr)))
        tail_audio = cropped_audio[st:en]
        if len(tail_audio) == 0:
            return None
        try:
            rms_val = float((tail_audio.astype("float64") ** 2).mean()) ** 0.5
        except Exception:
            return None
        if rms_val < self._DEAD_ZONE_RMS_FLOOR:
            return None  # 确实是真安静，不是死区，物理切点不需要调整

        # 在 [last_end, seg_dur] 这个区间内（换算到全局时间轴）找一个真正
        # 安静的位置，作为新的物理切点；复用与句子边界微调完全相同的
        # "安静阈值 + 连续安静区间"判定标准（_compute_silence_threshold /
        # _find_quiet_run_center），确保全文件"何为安静"的标准统一。
        last_end_global = start_sec + last_end_local
        nominal_end_global = start_sec + seg_dur
        silence_threshold = _compute_silence_threshold(global_rms)
        min_pause_frames = max(1, int(round(0.12 / hop_sec)))
        quiet_t = _find_quiet_run_center(
            global_rms, hop_sec, last_end_global, nominal_end_global,
            last_end_global, silence_threshold, min_pause_frames,
            prefer_longest=False,
        )
        if quiet_t is None:
            quiet_t = _find_quietest_point(
                global_rms, hop_sec, last_end_global, nominal_end_global,
            )
        # 找不到任何候选时，直接退到 last_end——把这一整截疑似人声原样
        # 让给下一段，既不生成额外的假静音，也不会丢失任何内容。
        new_boundary_global = quiet_t if quiet_t is not None else last_end_global
        new_boundary_global = max(last_end_global, min(new_boundary_global, nominal_end_global))

        logger.info(
            f"[Qwen3-FA][死区修复] 尾部 {tail_gap:.2f}s 确认存在真实语音"
            f"（RMS={rms_val:.4f}），把本段与下一段的物理切点从 "
            f"{nominal_end_global:.2f}s 收紧到 {new_boundary_global:.2f}s"
            "（本段对齐结果不变，多出的这一截音频整体让给下一段，"
            "不借用/不修改任何一段的参考文本）"
        )
        return new_boundary_global

    def _align_chunked(
        self,
        audio_path: str,
        text: str,
        lang_name: str,
        int_lang: str,
        total_sec: float,
        min_sentence_chunk_sec: float = 3.0,
        max_sentence_chunk_sec: float = 20.0,
    ) -> List[Tuple[float, float, str]]:
        """
        长音频分段对齐主流程：

          1. 读取整段音频，计算短时能量曲线。
          2. 按句子边界规划分段（_plan_sentence_aligned_chunks）：先按
             标点把参考文本切成完整的句子，默认每一句就是独立的一段
             （只有短于 min_sentence_chunk_sec 的过短句子才会与相邻句子
             合并；长于 max_sentence_chunk_sec 的过长句子会先按句内软
             停顿再切一刀），永远不在句子内部下刀；分段之间的物理切点
             再用短时能量曲线在句子边界附近微调到真实安静区间。参考
             文本完全没有标点、无法按句切分时，直接退回整段单次对齐
             （不再按音频时长/字符比例强行切分）。
          3. 逐段物理裁剪音频、写临时 wav，分别调用
             _align_single_chunk() 做局部对齐——每一段只知道自己的音频
             和自己的文本，互相之间完全独立（同 DialogueBatch.vue 批量
             独立对轨的思路一致）。
          4. 若某段结果末尾检测到"死区"（见 _resolve_trailing_dead_zone_
             boundary 顶部说明），只前移下一段的物理裁剪起点，不修改
             任何一段的参考文本。
          5. 局部时间戳 + 该段起始偏移 = 全局绝对时间戳，按顺序拼接
             （只做时间平移，不做文本补偿）。

        任何一步意外失败（音频读取失败、规划出的分段数 <= 1、某一段对齐
        异常等）都不会让整个任务失败：分别退化为"整段单次对齐"或"该段
        时长内均匀分配"，保证一定有可用的对齐结果返回。
        """
        import tempfile
        import shutil
        import os as _os

        try:
            import numpy as np
            import soundfile as sf
        except ImportError as e:
            # 【诊断】这个分支被命中时，静音感知分段会被完全跳过，退化为
            # "整段一次性喂给模型"——在长音频上会表现为一种和真实语句停顿
            # 完全无关、但周期看起来却相当规律的"伪分割"感（因为模型自身的
            # 音频编码器内部是按固定窗口分块处理长音频的，不具备静音感知
            # 能力）。PyInstaller onedir 打包最常见的触发方式：soundfile
            # 依赖的 libsndfile 动态库 / _soundfile_data 没有被正确收集进
            # 产物目录，导致打包后的 exe 里 import soundfile 直接失败，而
            # 开发环境的 venv 因为装的是完整 wheel 所以完全正常、复现不出来。
            # 用 logger.error + 醒目前缀，方便直接在日志里搜索
            # "QWEN3_FA_CHUNK_FALLBACK" 定位是否命中过这个分支。
            logger.error(
                f"[Qwen3-FA][QWEN3_FA_CHUNK_FALLBACK] 分段所需依赖缺失（{e}），"
                "本次长音频将不做静音感知分段、整段单次对齐；如果是打包后的 "
                "exe 里出现的，先检查 soundfile 的原生依赖（libsndfile 动态库 / "
                "_soundfile_data）是否被正确打进了 PyInstaller 产物目录"
            )
            return self._align_single_chunk(audio_path, text, lang_name, int_lang)

        try:
            audio, sr = sf.read(audio_path, always_2d=False)
        except Exception as e:
            # 同上：这里失败同样会导致静音感知分段被完全跳过。除了
            # soundfile 原生依赖缺失外，也可能是 mp3 之类需要 libsndfile
            # 具体 codec 支持的格式在打包后的运行环境里解码失败（常见于
            # libsndfile 版本/编译选项在打包时被替换或未随 DLL 一起打包）。
            logger.error(
                f"[Qwen3-FA][QWEN3_FA_CHUNK_FALLBACK] 分段所需的音频加载失败（{e}），"
                "本次长音频将不做静音感知分段、整段单次对齐；如果是打包后的 "
                "exe 里出现的，先检查音频格式（尤其是 mp3）在当前打包环境下 "
                "是否仍能被 soundfile/libsndfile 正常解码"
            )
            return self._align_single_chunk(audio_path, text, lang_name, int_lang)

        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)
        audio = np.asarray(audio, dtype=np.float32)

        rms, hop_sec = _compute_rms_curve(audio, sr)

        # 【WhisperX 粗测预处理，可选，默认关闭】见设置项
        # qwen3_fa_use_whisperx_prepass 说明与 _plan_chunks_via_whisperx_
        # rough_pass() 顶部说明：开启后先用 WhisperX 的 ASR 转录结果（真实
        # 语音边界）规划分段，只有在 WhisperX 不可用/失败/无法绑定参考
        # 文本时，才回退到下面 _plan_sentence_aligned_chunks() 这套按
        # 字符比例估算的旧方案——两条路径的输出格式完全一致，回退过程
        # 对调用方透明，不影响任务本身是否成功。
        spans: Optional[List[Tuple[float, float]]] = None
        chunk_texts: Optional[List[str]] = None
        prepass = _get_whisperx_prepass_settings()
        if prepass.get("enabled"):
            spans, chunk_texts = _plan_chunks_via_whisperx_rough_pass(
                audio_path, text, int_lang, total_sec, rms, hop_sec,
                min_sentence_chunk_sec, max_sentence_chunk_sec,
                whisper_model=str(prepass.get("whisper_model", "large-v3")),
                device=getattr(self, "_device", "auto"),
            )

        if spans is None:
            spans, chunk_texts = _plan_sentence_aligned_chunks(
                total_sec, rms, hop_sec, text, int_lang,
                min_sentence_chunk_sec, max_sentence_chunk_sec,
            )

        if len(spans) <= 1:
            logger.info("[Qwen3-FA] 规划结果无需分段，按整段单次对齐处理")
            return self._align_single_chunk(audio_path, text, lang_name, int_lang)

        logger.info(
            f"[Qwen3-FA] 音频切分为 {len(spans)} 段: "
            + ", ".join(f"[{s:.1f}s–{e:.1f}s]" for s, e in spans)
        )

        tmp_dir = tempfile.mkdtemp(prefix="qwen3_fa_chunk_")
        all_entries: List[Tuple[float, float, str]] = []
        try:
            # 注意：spans 在循环体内可能被就地修改（死区修复只前移"下一段"
            # 的起点，不会改变已经处理过的段），用 range(len(spans)) 顺序
            # 索引读取，而不是提前用 zip() 打包一份快照，确保每次都读到
            # 最新值。
            n_spans = len(spans)
            for idx in range(n_spans):
                start_sec, end_sec = spans[idx]
                chunk_text = (chunk_texts[idx] or "").strip()
                seg_dur = end_sec - start_sec

                st_samp = max(0, int(round(start_sec * sr)))
                en_samp = min(len(audio), int(round(end_sec * sr)))
                cropped = audio[st_samp:en_samp]

                local_entries: List[Tuple[float, float, str]] = []
                if not chunk_text:
                    logger.warning(
                        f"[Qwen3-FA] 第 {idx + 1}/{n_spans} 段未分配到参考文本，跳过对齐"
                    )
                elif len(cropped) < int(0.05 * sr):
                    logger.warning(
                        f"[Qwen3-FA] 第 {idx + 1}/{n_spans} 段裁剪后过短，跳过对齐"
                    )
                else:
                    chunk_wav = _os.path.join(tmp_dir, f"chunk_{idx:03d}.wav")
                    try:
                        sf.write(chunk_wav, cropped, sr)
                        # 每一段只喂给自己的音频 + 自己的文本，彼此完全
                        # 独立，互不知道对方的存在（不借用、不跨段修复）。
                        local_entries = self._align_single_chunk(chunk_wav, chunk_text, lang_name, int_lang)
                    except Exception as exc:
                        logger.error(
                            f"[Qwen3-FA] 第 {idx + 1}/{n_spans} 段对齐失败（{exc}），"
                            "该段退化为按时长均匀分配（精度较低）"
                        )
                        local_entries = []

                # 【尾部死区检测与修复】见 _resolve_trailing_dead_zone_boundary
                # 顶部说明：这一段自己已经对齐出结果，但结果末尾比这一段
                # 自己的音频总时长明显短一截，且那一截里其实还有真实
                # 人声——大概率是下一句话提前说出来的开头部分，物理上被
                # 划进了当前段。只前移下一段的物理裁剪起点，不修改任何
                # 一段的参考文本（不借用、不重新对齐、不做数量估计）。
                if local_entries and idx + 1 < n_spans:
                    new_boundary = self._resolve_trailing_dead_zone_boundary(
                        local_entries, cropped, sr, start_sec, seg_dur, rms, hop_sec,
                    )
                    if new_boundary is not None:
                        next_start, next_end = spans[idx + 1]
                        # 只收紧，不越界：新边界必须落在 [本段起点, 下一段
                        # 终点) 之间，避免下一段音频被挤压成空/负长度。
                        new_boundary = max(start_sec, min(new_boundary, next_end - 0.05))
                        spans[idx + 1] = (new_boundary, next_end)

                if not local_entries and chunk_text:
                    # 降级：该段内按可发音单元均匀分配时长，保证时间轴不断裂。
                    units = (
                        list(chunk_text) if int_lang in ("zh", "yue", "ja", "ko")
                        else chunk_text.split()
                    )
                    units = [u for u in units if u.strip() and not _is_cjk_punct(u)]
                    if units:
                        dur = seg_dur / len(units)
                        local_entries = [
                            (i * dur, (i + 1) * dur, u) for i, u in enumerate(units)
                        ]

                for s, e, tok in local_entries:
                    all_entries.append((s + start_sec, e + start_sec, tok))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        all_entries.sort(key=lambda x: x[0])
        return all_entries

    def _extract_timestamps(
        self,
        outputs,
        text: str,
        int_lang: str,
        total_sec: float,
    ) -> List[Tuple[float, float, str]]:
        import torch

        entries: List[Tuple[float, float, str]] = []

        # ── 方案 A：CTC logits + torchaudio forced_align ─────────────────
        if hasattr(outputs, "logits"):
            try:
                import torchaudio
                logits    = outputs.logits[0].float()
                log_probs = torch.nn.functional.log_softmax(logits, dim=-1)
                vocab     = self._processor.tokenizer.get_vocab()
                tokens    = self._processor.tokenizer(
                    text, return_tensors="pt"
                ).input_ids[0]
                frame_dur = total_sec / logits.shape[0]

                if hasattr(torchaudio.functional, "forced_align"):
                    aligned = torchaudio.functional.forced_align(
                        log_probs.unsqueeze(0).cpu(),
                        tokens.unsqueeze(0),
                        blank=vocab.get("<pad>", 0),
                    )
                    spans = torchaudio.functional.merge_tokens(
                        aligned[0][0], self._processor.tokenizer.pad_token_id
                    )
                    id2tok = {v: k for k, v in vocab.items()}
                    for span in spans:
                        tok = id2tok.get(span.token, "").lstrip("▁").strip()
                        if tok:
                            entries.append((
                                span.start * frame_dur,
                                span.end   * frame_dur,
                                tok,
                            ))
                else:
                    # 无 forced_align：贪婪 CTC 解码
                    pred_ids = torch.argmax(logits, dim=-1).cpu().numpy()
                    blank_id = vocab.get("<pad>", 0)
                    id2tok   = {v: k for k, v in vocab.items()}
                    cur_tok, cur_start = None, 0
                    for i, tid in enumerate(pred_ids):
                        if int(tid) == blank_id:
                            if cur_tok is not None:
                                tok = id2tok.get(cur_tok, "").lstrip("▁").strip()
                                if tok:
                                    entries.append((
                                        cur_start * frame_dur, i * frame_dur, tok
                                    ))
                                cur_tok = None
                        else:
                            if cur_tok != int(tid):
                                if cur_tok is not None:
                                    tok = id2tok.get(cur_tok, "").lstrip("▁").strip()
                                    if tok:
                                        entries.append((
                                            cur_start * frame_dur, i * frame_dur, tok
                                        ))
                                cur_tok, cur_start = int(tid), i
                    if cur_tok is not None:
                        tok = id2tok.get(cur_tok, "").lstrip("▁").strip()
                        if tok:
                            entries.append((
                                cur_start * frame_dur,
                                len(pred_ids) * frame_dur,
                                tok,
                            ))
            except Exception as e:
                logger.warning(f"[Qwen3-FA] CTC 时间戳提取失败: {e}")

        # ── 方案 B：Seq2Seq timestamp token ──────────────────────────────
        if not entries and hasattr(outputs, "sequences"):
            try:
                decoded = self._processor.batch_decode(
                    outputs.sequences, output_offsets=True, skip_special_tokens=True
                )
                for item in decoded:
                    if isinstance(item, dict) and "chunks" in item:
                        for chunk in item["chunks"]:
                            ts  = chunk.get("timestamp") or (None, None)
                            tok = (chunk.get("text") or "").strip()
                            if ts[0] is not None and tok:
                                entries.append((
                                    float(ts[0]),
                                    float(ts[1] or ts[0] + 0.25),
                                    tok,
                                ))
            except Exception as e:
                logger.warning(f"[Qwen3-FA] Seq2Seq 时间戳提取失败: {e}")

        # ── 降级：均匀分配 ────────────────────────────────────────────────
        if not entries:
            logger.warning("[Qwen3-FA] 无法提取时间戳，改用均匀分配（精度较低）")
            # 【修复】同 Qwen3ASRAligner：补上 "ko"，韩语走逐字符均分而非按空格切词组
            units = list(text) if int_lang in ("zh", "yue", "ja", "ko") else text.split()
            units = [u for u in units if u.strip() and not _is_cjk_punct(u)]
            if units:
                dur = total_sec / len(units)
                entries = [(i * dur, (i + 1) * dur, u) for i, u in enumerate(units)]

        return entries


# ═════════════════════════════════════════════════════════════════════════════
# 8. NeMoForcedAligner
#    https://github.com/NVIDIA-NeMo/Speech/tree/main/tools/nemo_forced_aligner
#
#    与 Qwen3ASRAligner 同样的理由和同样的做法：nemo_toolkit[asr] 对
#    packaging / fsspec / omegaconf / hydra-core / lightning 等核心依赖
#    有严格的版本限制，装进主进程所在的 .mfa_env 会跟其它包发生版本冲突
#    （实测会把 packaging 降级到 24.x，导致 pipdeptree 等工具报错）。
#
#    因此本类不在当前进程内 import nemo，而是作为 HTTP 客户端调用一个
#    独立的 nemo_server.py 微服务（运行在独立 conda/venv 环境里，默认端口
#    127.0.0.1:5002），真正的模型加载、CTC log-probs 计算、
#    torchaudio.functional.forced_align Viterbi 强制对齐全部在那个独立
#    进程里完成，这边只负责发请求、拿 token_entries、合并成 LAB。
#
#    官方 NFA 本身是一个基于 manifest + Hydra config 的批处理 CLI 工具
#    (align.py)，内部原理与 nemo_server.py 里实现的完全一致：
#      1. 加载一个 NeMo CTC / Hybrid-CTC-Transducer（CTC 模式）ASR 模型
#      2. 对音频做一次前向传播，取每帧 log-softmax 输出（log-probs）
#      3. 用 Viterbi 算法在 log-probs 上对参考文本的 token 序列做强制对齐
#      4. 输出 token 级时间戳
#    只是省去了 manifest/CTM 文件落盘与 Hydra 配置这一层。
#
#    部署方式（与 qwen3_server.py 完全一致的模式）：
#      conda create -n nemo_env python=3.10 -y
#      conda activate nemo_env
#      pip install "nemo_toolkit[asr]>=2.7.0,<2.8.0"
#      python nemo_server.py     # 默认监听 127.0.0.1:5002
#    然后正常启动主 Flask 应用（.mfa_env），选择 nemo_aligner 后端即可。
# ═════════════════════════════════════════════════════════════════════════════

class NeMoForcedAligner(AltAlignerBase):
    """
    NeMo Forced Aligner (NFA) 独立服务客户端
    ────────────────────────────────────────
    只通过 HTTP 调用 nemo_server.py，不在当前进程内加载 NeMo / import nemo。
    与 Qwen3ASRAligner 是同一种部署形态：模型推理在独立环境的另一个
    Flask 微服务里完成，这里只是个轻客户端。

    支持语言（与 nemo_server.py 的 LANGUAGE_MODELS 保持一致，仅供前端展示，
    真正生效的模型表在服务端）：
      en  stt_en_fastconformer_hybrid_large_pc   (Hybrid, NGC, 走 CTC 模式)
      zh  nvidia/stt_zh_citrinet_1024_gamma_0_25  (纯 CTC, HuggingFace Hub)
      ja  nvidia/parakeet-tdt_ctc-0.6b-ja          (Hybrid TDT+CTC, 走 CTC 模式)
    韩语 / 粤语目前没有 NVIDIA 官方发布的 CTC 或 Hybrid-CTC checkpoint，
    服务端会在 /align 接口返回 400 + 清晰错误信息，而不是套用不兼容的
    模型类型硬跑。
    """

    DEFAULT_ENDPOINT = "http://127.0.0.1:5002/align"

    # 仅用于前端展示 / 状态接口，不影响实际对齐逻辑（真正的模型选择逻辑在
    # nemo_server.py 里，因为模型必须在那个独立进程里加载）。
    LANGUAGE_MODELS: Dict[str, str] = {
        "en": "stt_en_fastconformer_hybrid_large_pc",
        "zh": "nvidia/stt_zh_citrinet_1024_gamma_0_25",
        "ja": "nvidia/parakeet-tdt_ctc-0.6b-ja",
    }

    def __init__(
        self,
        device: str = "auto",
        nemo_model: Optional[str] = None,
        endpoint: str = DEFAULT_ENDPOINT,
        batch_size: int = 8,
    ):
        super().__init__()
        self._device = device
        self._model_override = nemo_model
        self.endpoint = endpoint.rstrip("/")
        self._session = None
        # 透传给 nemo_server.py /align 请求体里的 "batch_size" 字段（见该
        # 服务端文件顶部说明：NeMo Forced Aligner 单次前向没有真正意义
        # 上可调的批大小，这个值仅在显存不足自动降级重试时写入日志作为
        # 参考）。默认 8，与 app_settings.DEFAULT_SETTINGS["qwen3_batch_size"]
        # 一致。
        self.batch_size = max(1, int(batch_size))

    @staticmethod
    def check_available() -> Tuple[bool, str]:
        try:
            import requests  # noqa: F401
        except ImportError as e:
            return False, f"未安装 requests: pip install requests ({e})"

        try:
            r = requests.get("http://127.0.0.1:5002/", timeout=2)
            return True, "NeMo Forced Aligner 独立服务已可访问"
        except Exception as e:
            return False, f"NeMo Forced Aligner 独立服务不可访问: {e}"

    def _load_model(self):
        """独立服务模式下不加载本地模型，只做轻量级连接初始化。"""
        if self._session is None:
            self._session = requests.Session()

    def _call_nemo_service(self, audio_path: str, text: str, language: str) -> Dict:
        self._load_model()

        payload = {
            "audio": audio_path,
            "text": text,
            "language": language,
            "device": self._device if self._device in ("auto", "cpu", "cuda") else "auto",
            "batch_size": self.batch_size,
        }
        if self._model_override:
            payload["model"] = self._model_override

        resp = self._session.post(self.endpoint, json=payload, timeout=1800)
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success", False):
            raise RuntimeError(data.get("error", "NeMo Forced Aligner 服务返回失败"))

        return data

    # ── 主对齐入口 ────────────────────────────────────────────────────────
    def align(self, audio_path: str, text: Optional[str], language: str,
              english_word_align: bool = False) -> Dict:
        """
        执行 NeMo Forced Aligner 对齐，返回与 MFAProcessor.process()
        格式兼容的字典。NFA 必须提供参考文本（requires_text=True）。
        """
        t0 = time.time()

        if not text or not text.strip():
            return {
                "success": False,
                "error": "NeMo Forced Aligner 需要提供参考文本（requires_text=True）",
                "processing_time": 0,
            }

        try:
            int_lang = _normalize_lang(language)
            clean_text = _clean_align_text(text)

            logger.info(f"[NeMo-FA] 调用独立服务: {self.endpoint}")
            result = self._call_nemo_service(audio_path, clean_text, int_lang)

            raw_entries = result.get("token_entries") or []
            token_entries: List[Tuple[float, float, str]] = [
                (float(s), float(e), str(tok))
                for s, e, tok in raw_entries
                if e is not None and s is not None and e > s and str(tok).strip()
            ]

            if not token_entries:
                return {
                    "success": False,
                    "error": "[NeMo-FA] 强制对齐未产生任何时间戳条目",
                    "processing_time": int((time.time() - t0) * 1000),
                }

            model_used = result.get("model", "unknown")
            logger.info(
                f"[NeMo-FA] ✓ 服务端返回 {len(token_entries)} 个 token spans，"
                f"使用模型: {model_used}"
            )

            # 【停顿策略修正】不同 NeMo 模型的帧时长差异很大（citrinet 约
            # 79ms/frame，fastconformer/parakeet 量级 ~20–40ms/frame）。
            # fill_silences=True 用的是 _fill_silences_lab() 里固定写死的
            # 50ms 阈值——citrinet 单帧时长本身就已经超过这个阈值，于是
            # 几乎每两个相邻字符之间正常的帧量化间隙（nemo_server.py 已
            # 把真正的 blank 帧时长向左合并进了前一个字符，不再产生游离
            # 空隙），只要因为浮点取整等原因残留一点点间隙，也会被一刀切
            # 误判成"真实停顿"，在 SVP 里表现为几乎每个字都被强行隔开
            # （参见用户截图：单字之间出现明显空拍）。
            #
            # 改为与 WhisperX 完全一致的停顿策略：不依赖时间间隙启发式，
            # 而是直接按参考文本里的标点位置确定性地插入停顿
            # （_inject_sentence_pauses，句末标点 80ms / 句内标点 40ms），
            # 再用 _fix_ctc_stretch 兜底截断任何异常超长的单字（防止合并
            # 后某个字因为吸收了过长的停顿帧而变成一个不合理的长音），
            # 最后做一次音素时长守护（PDG）避免出现过短音标。
            # fill_silences 改为 False，因为停顿已经由上面几步显式写入，
            # 不需要再做一次基于时间间隙的全局扫描（也不应该再做，否则
            # 又会回到"逐字插入 SIL"的老问题）。
            token_entries = _inject_sentence_pauses(token_entries, text)
            token_entries = _fix_ctc_stretch(token_entries, int_lang)
            token_entries = WhisperXAligner._apply_duration_guard(token_entries)

            lab = self._word_entries_to_lab(
                token_entries, text, language,
                fill_silences=False,
                english_word_align=english_word_align,
            )

            return {
                "success": True,
                "lab_content": lab,
                "raw_text": text,
                "phoneme_text": clean_text,
                "audio_duration": self._get_audio_duration_100ns(audio_path),
                "processing_time": int((time.time() - t0) * 1000),
                "backend": "nemo_aligner",
            }

        except Exception as e:
            logger.error(f"[NeMo-FA] 失败: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "processing_time": int((time.time() - t0) * 1000),
            }


# ═════════════════════════════════════════════════════════════════════════════
# 9. 单例缓存与工厂函数
# ═════════════════════════════════════════════════════════════════════════════

_SINGLETON: Dict[str, AltAlignerBase] = {}


def get_aligner(backend: str, device: str = "auto", **kwargs) -> AltAlignerBase:
    global _SINGLETON
    resolved_device = _safe_device(device)   # 提前解析一次，用真实设备做 key

    if backend == "whisperx":
        whisper_model = kwargs.pop("whisper_model", "large-v3")
        batch_size = kwargs.get("batch_size", 16)
        cache_key = f"whisperx:{whisper_model}:{resolved_device}:{batch_size}"
        if cache_key not in _SINGLETON:
            _SINGLETON[cache_key] = WhisperXAligner(
                whisper_model=whisper_model, device=device, **kwargs
            )
        return _SINGLETON[cache_key]

    if backend == "nemo_aligner":
        nemo_model = kwargs.pop("nemo_model", None)
        # batch_size 变化也要重新建实例——虽然对 NeMo-FA 而言它只是显存
        # 不足自动降级时写日志用的参考值，不影响推理结果，但缓存 key 里
        # 带上它可以让"设置页面刚保存的新 batch_size"立即在下一次任务里
        # 反映到日志中，不需要等到设备也变化才会重新实例化。
        batch_size = kwargs.get("batch_size", 8)
        cache_key = f"nemo_aligner:{nemo_model or 'default'}:{resolved_device}:{batch_size}"
        if cache_key not in _SINGLETON:
            _SINGLETON[cache_key] = NeMoForcedAligner(
                device=device, nemo_model=nemo_model, **kwargs
            )
        return _SINGLETON[cache_key]

    if backend in ("qwen3_asr", "qwen3_aligner"):
        # 同上：batch_size 纳入缓存 key，确保设置变更后下一次任务立即用
        # 新值重新实例化，不需要等设备也一起变化。
        batch_size = kwargs.get("batch_size", 8)
        cache_key = f"{backend}:{resolved_device}:{batch_size}"
        if cache_key not in _SINGLETON:
            if backend == "qwen3_asr":
                _SINGLETON[cache_key] = Qwen3ASRAligner(device=device, **kwargs)
            else:
                _SINGLETON[cache_key] = Qwen3ForcedAligner(device=device, **kwargs)
        return _SINGLETON[cache_key]

    raise ValueError(f"未知对齐后端: {backend}")


def clear_aligner_cache(backend: Optional[str] = None) -> int:
    """
    清理单例缓存，释放旧设备/旧配置留下的实例（及其显存/内存占用）。

    - backend=None: 清空所有缓存的 aligner 实例
    - backend="whisperx" / "nemo_aligner" / "qwen3_asr" / "qwen3_aligner":
      只清理该 backend 前缀对应的所有缓存条目（不同 model/device/batch_size 的都会被清掉）
    返回被清理的条目数。
    """
    global _SINGLETON
    if backend is None:
        n = len(_SINGLETON)
        _SINGLETON.clear()
        return n

    keys_to_drop = [k for k in _SINGLETON if k == backend or k.startswith(f"{backend}:")]
    for k in keys_to_drop:
        del _SINGLETON[k]
    return len(keys_to_drop)


def get_alt_aligner_status() -> Dict:
    """检查所有替代对齐后端的可用状态（含模型文件目录信息）"""
    wx_ok, wx_msg = WhisperXAligner.check_available()
    qa_ok, qa_msg = Qwen3ASRAligner.check_available()
    qf_ok, qf_msg = Qwen3ForcedAligner.check_available()
    nm_ok, nm_msg = NeMoForcedAligner.check_available()

    return {
        "models_dir": str(_MODELS_DIR),        # ← 前端可展示此路径
        "whisperx": {
            "available":       wx_ok,
            "message":         wx_msg,
            "requires_text":   False,
            "description":     "WhisperX (Whisper ASR + wav2vec2 强制对齐)",
            "default_model":   "large-v3",
            "supported_models": WhisperXAligner.SUPPORTED_MODELS,
        },
        "qwen3_asr": {
            "available":     qa_ok,
            "message":       qa_msg,
            "requires_text": False,
            "description":   "Qwen3-ASR-1.7B (自动语音识别)",
            "model_paths": {
                "hf_cache": str(_HF_HUB),
            },
        },
        "qwen3_aligner": {
            "available":     qf_ok,
            "message":       qf_msg,
            "requires_text": True,
            "description":   "Qwen3-ForcedAligner-0.6B (强制对齐)",
            "model_paths": {
                "hf_cache": str(_HF_HUB),
            },
        },
        "nemo_aligner": {
            "available":       nm_ok,
            "message":         nm_msg,
            "requires_text":   True,
            "description":     "NeMo Forced Aligner (独立服务，需先启动 nemo_server.py)",
            "language_models": NeMoForcedAligner.LANGUAGE_MODELS,
            "endpoint":        NeMoForcedAligner.DEFAULT_ENDPOINT,
        },
    }