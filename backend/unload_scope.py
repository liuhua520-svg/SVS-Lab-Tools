# -*- coding: utf-8 -*-
"""
「用完即卸」的批量作用域：把"每个子任务结束就卸载"推迟到"整批任务的某个边界"。

背景
────
「用完即卸」原本按"一次调用"为粒度触发：对话文本框批量处理里有 N 个对话框，
就会 加载 → 对齐 → 卸载 重复 N 次（RMVPE / CREPE 的 F0 提取同理）。
对话框越多，反复加载模型的开销越大。

用法
────
1. 批量入口（pipeline.process_dialogue_batch）用 `with defer_unloads():` 包住
   整个批处理；作用域内所有"用完即卸"触发点改调 `unload_or_defer()`，
   不再立即卸载，而是登记为待卸载项。
2. 对齐阶段全部结束后调用 `flush_deferred_unloads("aligner")`，一次性卸载
   所有对齐模型，再开始 F0 提取（显存紧张的显卡上，对齐模型必须先让出显存）。
3. 退出作用域时（无论正常结束、取消还是异常）自动 `flush_deferred_unloads()`，
   卸载剩余的（F0 模型等）。

作用域外调用 `unload_or_defer()` 等价于立即执行卸载函数，
所以单文件处理、TTS、字幕对齐等其它路径的行为完全不变。

线程语义
────────
作用域状态存放在 threading.local 里，只对"进入作用域的那个线程"生效，
不会让其它线程里并发运行的独立任务的卸载被意外推迟。
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_local = threading.local()

# 分组名常量，避免调用方手写字符串出错
GROUP_ALIGNER = "aligner"   # Qwen3-ASR / Qwen3-FA / WhisperX / NeMo-FA
GROUP_F0 = "f0"             # RMVPE / CREPE


def _get_state():
    if not hasattr(_local, "depth"):
        _local.depth = 0
        # (group, key) -> callable；同一 key 重复登记只保留一份（最后一次为准）
        _local.pending = {}
    return _local


def is_deferring() -> bool:
    """当前线程是否处于 defer_unloads() 作用域内。"""
    return _get_state().depth > 0


def unload_or_defer(group: str, key: str, fn: Callable[[], object]) -> bool:
    """
    作用域内：登记为待卸载项，返回 True（已推迟）。
    作用域外：立即执行 fn()，返回 False（与改造前行为一致）。

    key 用来去重：同一个模型在一批里被触发 N 次，只会卸载一次。
    """
    st = _get_state()
    if st.depth > 0:
        st.pending[(group, key)] = fn
        logger.debug("[用完即卸] 批量作用域内推迟卸载: %s/%s", group, key)
        return True
    fn()
    return False


def flush_deferred_unloads(group: Optional[str] = None) -> int:
    """
    执行并清除待卸载项。group=None 表示全部；否则只处理指定分组。
    单个卸载失败只记日志，不影响其它项，也不向上抛异常。
    返回实际执行的卸载项个数。
    """
    st = _get_state()
    todo: Dict[Tuple[str, str], Callable[[], object]] = {
        k: fn for k, fn in st.pending.items() if group is None or k[0] == group
    }
    for k in todo:
        st.pending.pop(k, None)

    for (g, key), fn in todo.items():
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - 卸载失败不应影响任务结果
            logger.warning("[用完即卸] 批量结束后卸载 %s/%s 失败（不影响任务结果）: %s", g, key, e)
    return len(todo)


@contextmanager
def defer_unloads():
    """
    进入批量作用域（可嵌套，只有最外层退出时才会兜底清空全部待卸载项）。
    """
    st = _get_state()
    st.depth += 1
    try:
        yield
    finally:
        st.depth -= 1
        if st.depth == 0:
            flush_deferred_unloads()
