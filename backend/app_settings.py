# -*- coding: utf-8 -*-
"""
应用级设置管理：模型自动更新开关（HF_HUB_OFFLINE）、镜像站下载配置（HF_ENDPOINT），
以及 alt_aligners.py 里一批可调的对齐后处理调优参数。

设计说明
────────
【HF_HUB_OFFLINE / HF_ENDPOINT】
这两个环境变量分别由三个各自独立的进程消费：
  - app.py（经由其在模块顶层 import 的 alt_aligners.py）
  - qwen3_server.py（独立 venv 子服务，端口 5001）
  - nemo_server.py（独立 venv 子服务，端口 5002）

三者都会在各自最早的时机（import huggingface_hub / transformers /
qwen_asr / nemo 之前）调用本模块的 apply_env_from_settings()，从同一份
JSON 配置文件读取设置并写入 os.environ。这样设置页面只需要写一次文件，
三个进程各自重启后即可生效——环境变量只在"进程启动时"读取一次，
所以修改设置后必须重启对应进程（尤其是 Qwen3 / NeMo 两个微服务）才能
让新配置真正生效，这一点在设置页面的提示文案里需要向用户说清楚。

【对齐调优参数】
与上面两个环境变量不同，以下这批参数只被 alt_aligners.py 消费，而
alt_aligners.py 只在主进程（app.py）里被 import——不涉及 Qwen3 / NeMo
两个独立子服务，因此**不需要重启任何进程**：alt_aligners.py 通过本模块的
get_alignment_tuning() 在每次调用时实时读取最新值（内部仍有轻量缓存，
文件 mtime 变化时自动刷新），保存设置后下一次对齐任务即可生效。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = PROJECT_DIR / "settings" / "app_settings.json"

_lock = threading.RLock()

DEFAULT_SETTINGS: Dict[str, object] = {
    # False → HF_HUB_OFFLINE=1（允许 huggingface_hub 联网检查/下载模型）
    # True  → HF_HUB_OFFLINE=0（默认禁用自动联网更新，行为与改造前一致，最省心）
    "auto_update_models": False,
    "use_mirror": False,
    "mirror_url": "https://hf-mirror.com/",

    # True  → 尝试隐藏 app.py / qwen3_server.py / nemo_server.py 三个进程
    #         各自所在的命令提示符（终端）窗口；进程本身继续正常运行，
    #         只是窗口不可见。
    # False → 保持命令提示符窗口可见（默认，方便直接看日志）。
    # 仅在 Windows 上生效，见 apply_console_visibility() 的说明。
    "hide_console_window": False,

    # True  → 下次完整启动本应用（即重新打开 exe 启动器）时，不再自动拉起
    #         qwen3_server.py / nemo_server.py 对应的那个微服务进程。
    # False → 下次启动时正常拉起（默认）。
    # 与 hide_console_window 不同，这两项不会触发/也不需要触发任何"立即
    # 生效"的动作——它们只在下一次完整启动应用（即启动器重新拉起三个子
    # 进程）时被读取一次，对当前已经在运行的进程没有任何影响，保存设置
    # 本身也不会关闭正在运行的服务。具体的"跳过启动"逻辑在启动器
    # (launcher.py) 里实现：它在拉起子进程之前读取这两个字段。
    "skip_start_qwen3_server": False,
    "skip_start_nemo_server": False,

    # ── alt_aligners.py 对齐调优参数（默认值与原硬编码常量保持一致）────────
    # Qwen3-ForcedAligner / Qwen3-ASR 全局事后偏移校正（秒）。
    "qwen3_fa_onset_delay_sec": 0.06,
    "qwen3_asr_onset_delay_sec": 0.06,
    # 偏移校正后允许的最短音节时长（秒），避免被压成 0 或负数。
    "qwen3_fa_min_syl_dur_sec": 0.02,
    "qwen3_asr_min_syl_dur_sec": 0.02,
    # CTC 短语边界拉伸修复：各类 token 的时长上限 / 最小可插入 SP 时长（秒）。
    "ctc_max_cjk_char_sec": 0.50,
    "ctc_max_cjk_particle_sec": 0.35,
    "ctc_max_en_word_sec": 1.20,
    "ctc_min_sp_sec": 0.15,

    # Qwen3-ForcedAligner 长音频分段对齐（详见 alt_aligners.py 里
    # Qwen3ForcedAligner._align_chunked() 的说明）。
    # 音频总时长超过该阈值（秒）时，自动切分成多段分别对齐，避免长音频
    # 一次性喂给模型导致时间戳随时长推进逐渐漂移/错位。
    "qwen3_fa_chunk_threshold_sec": 120.0,
    # 每段的目标长度（秒）。实际切点会在 [目标长度×0.5, 目标长度×1.5]
    # 区间内自动吸附到音频能量最低（最可能是真实停顿）的位置，不会严格
    # 等于这个数值。
    "qwen3_fa_chunk_target_sec": 100.0,
}

# 对齐调优参数的合法取值范围（秒），用于 save_settings() 里的边界钳制。
# 上限给得比较宽松，只用来挡住明显的输入错误（比如误输成毫秒级数字 60）。
_TUNING_RANGES: Dict[str, tuple] = {
    "qwen3_fa_onset_delay_sec": (0.0, 2.0),
    "qwen3_asr_onset_delay_sec": (0.0, 2.0),
    "qwen3_fa_min_syl_dur_sec": (0.0, 1.0),
    "qwen3_asr_min_syl_dur_sec": (0.0, 1.0),
    "ctc_max_cjk_char_sec": (0.05, 5.0),
    "ctc_max_cjk_particle_sec": (0.05, 5.0),
    "ctc_max_en_word_sec": (0.05, 5.0),
    "ctc_min_sp_sec": (0.0, 2.0),
    # 0 表示禁用分段（即使音频再长也整段单次对齐，等同于改造前的行为）。
    "qwen3_fa_chunk_threshold_sec": (0.0, 3600.0),
    "qwen3_fa_chunk_target_sec": (0.0, 300.0),
}

# alt_aligners.get_alignment_tuning() 使用的轻量缓存：避免每次对齐调用都
# 重新读盘/解析 JSON。用文件 mtime 判断是否需要刷新，保存设置后下一次
# 对齐任务立即生效，无需重启任何进程。
_tuning_cache_lock = threading.RLock()
_tuning_cache: Dict[str, float] = {}
_tuning_cache_mtime: float = -1.0


def load_settings() -> Dict[str, object]:
    with _lock:
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                merged = dict(DEFAULT_SETTINGS)
                if isinstance(data, dict):
                    merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
                return merged
            except Exception as e:
                logger.error("读取设置文件失败（%s），使用默认设置: %s", SETTINGS_PATH, e)
                return dict(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)


def save_settings(new_settings: Dict[str, object]) -> Dict[str, object]:
    with _lock:
        current = load_settings()
        current.update({k: v for k, v in (new_settings or {}).items() if k in DEFAULT_SETTINGS})

        # 基本校验，避免脏数据写入
        current["auto_update_models"] = bool(current.get("auto_update_models"))
        current["use_mirror"] = bool(current.get("use_mirror"))
        mirror_url = str(current.get("mirror_url") or "").strip()
        current["mirror_url"] = mirror_url or DEFAULT_SETTINGS["mirror_url"]
        current["hide_console_window"] = bool(current.get("hide_console_window"))
        current["skip_start_qwen3_server"] = bool(current.get("skip_start_qwen3_server"))
        current["skip_start_nemo_server"] = bool(current.get("skip_start_nemo_server"))

        # 对齐调优参数：转 float 并钳制到 _TUNING_RANGES 定义的合法区间，
        # 非法/缺失/无法解析的值一律回退为默认值，避免脏数据写入导致
        # alt_aligners.py 后续计算出负数时长等异常。
        for key, (lo, hi) in _TUNING_RANGES.items():
            raw = current.get(key, DEFAULT_SETTINGS[key])
            try:
                val = float(raw)
                if val != val:  # NaN
                    raise ValueError("NaN")
            except (TypeError, ValueError):
                logger.warning("设置项 %s 的值 %r 无效，回退为默认值 %s", key, raw, DEFAULT_SETTINGS[key])
                val = float(DEFAULT_SETTINGS[key])
            current[key] = min(max(val, lo), hi)

        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(SETTINGS_PATH)

        # 对齐调优参数已随本次保存写入磁盘；让 get_alignment_tuning() 的
        # 缓存立即失效，这样即使当前请求处理进程与下一次对齐任务用的是
        # 同一进程内的缓存副本，也不会读到过期值（正常情况下 mtime 检测
        # 已经足够，这里是双保险，成本很低）。
        global _tuning_cache_mtime
        with _tuning_cache_lock:
            _tuning_cache_mtime = -1.0

        return current


def apply_env_from_settings() -> Dict[str, object]:
    """
    在进程启动早期调用：把设置文件内容映射为环境变量。

    必须在 import huggingface_hub / transformers / qwen_asr / nemo_toolkit
    之前调用，这些库只在 import 时读取一次 HF_HUB_OFFLINE / HF_ENDPOINT，
    之后修改 os.environ 不会再生效（需要重启进程）。
    """
    settings = load_settings()

    # 逻辑反转：当前端开关打开（True）代表用户想要“禁用更新”，所以设置为 "1"
    os.environ["HF_HUB_OFFLINE"] = "1" if settings.get("auto_update_models") else "0"

    if settings.get("use_mirror") and settings.get("mirror_url"):
        os.environ["HF_ENDPOINT"] = str(settings["mirror_url"]).rstrip("/") + "/"
    else:
        os.environ.pop("HF_ENDPOINT", None)

    logger.info(
        "已应用模型下载设置: HF_HUB_OFFLINE=%s, HF_ENDPOINT=%s",
        os.environ.get("HF_HUB_OFFLINE"),
        os.environ.get("HF_ENDPOINT", "(未设置，使用官方 huggingface.co)"),
    )
    return settings


def apply_console_visibility() -> bool:
    """
    显示或隐藏当前进程所在的命令提示符（控制台）窗口，取决于设置文件里
    hide_console_window 的当前值。

    与 apply_env_from_settings() 不同，这里不是"只能在启动早期调用一次"：
    ShowWindow() 可以在进程运行期间随时调用，每次调用都会把窗口设为当前
    设置要求的显示/隐藏状态，是完全可逆、可重复调用的操作——不会杀掉进程，
    也不影响 Flask 服务本身，只是这个窗口本身在任务栏/桌面上看不看得见。

    调用时机：
      - app.py：在 main() 启动时调用一次即可覆盖"launcher 打开时窗口是否
        隐藏"；另外在 /api/settings 保存设置时也会立即调用一次——这样切换
        开关后，主进程自己的窗口无需重启即可立刻隐藏或恢复显示。
      - qwen3_server.py / nemo_server.py：在模块导入阶段、紧跟
        apply_env_from_settings() 之后调用一次。这两个微服务的 /restart
        本来就会在保存设置后自动重新拉起一个全新进程（用于让
        HF_HUB_OFFLINE / HF_ENDPOINT 生效），新进程启动时会重新读取到最新
        的 hide_console_window 值并据此隐藏/显示窗口，无需额外处理。

    仅在 Windows 上有效（用到 kernel32 / user32 的 GetConsoleWindow /
    ShowWindow）；非 Windows 平台直接跳过并返回 False。

    Returns
    -------
    bool：本次是否成功找到控制台窗口并设置了显示状态。False 常见于：非
    Windows 平台、或找不到控制台窗口（例如从某些 IDE 的集成终端启动、或
    stdout 被完全重定向导致没有关联的控制台）——这两种情况都不算错误。
    """
    if os.name != "nt":
        return False
    try:
        settings = load_settings()
        hide = bool(settings.get("hide_console_window"))

        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return False

        SW_HIDE = 0
        SW_SHOW = 5
        ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE if hide else SW_SHOW)
        logger.info("已将控制台窗口设置为: %s", "隐藏" if hide else "显示")
        return True
    except Exception as e:
        logger.warning("设置控制台窗口显示状态失败（不影响服务本身运行）: %s", e)
        return False


def get_alignment_tuning() -> Dict[str, float]:
    """
    供 alt_aligners.py 调用：实时读取一批对齐后处理调优参数（均为秒）。

    与 apply_env_from_settings() 不同，这里刻意不缓存到 os.environ、也不要求
    在进程启动早期调用——alt_aligners.py 只在主进程内运行，每次对齐任务
    调用本函数即可拿到最新值，保存设置后无需重启任何进程。

    内部用文件 mtime 做了一层缓存：同一个 app_settings.json 没有变化时，
    重复调用不会重新读盘/解析 JSON（对齐流程里同一次任务可能会多次用到
    这些参数）。文件不存在（尚未保存过任何设置）时返回内置默认值。

    Returns
    -------
    Dict[str, float]，键固定为 _TUNING_RANGES 中的 10 个参数名。
    """
    global _tuning_cache, _tuning_cache_mtime

    try:
        mtime = SETTINGS_PATH.stat().st_mtime if SETTINGS_PATH.exists() else 0.0
    except OSError:
        mtime = 0.0

    with _tuning_cache_lock:
        if _tuning_cache and mtime == _tuning_cache_mtime:
            return dict(_tuning_cache)

    settings = load_settings()
    tuning: Dict[str, float] = {}
    for key in _TUNING_RANGES:
        try:
            tuning[key] = float(settings.get(key, DEFAULT_SETTINGS[key]))
        except (TypeError, ValueError):
            tuning[key] = float(DEFAULT_SETTINGS[key])

    with _tuning_cache_lock:
        _tuning_cache = dict(tuning)
        _tuning_cache_mtime = mtime

    return tuning
