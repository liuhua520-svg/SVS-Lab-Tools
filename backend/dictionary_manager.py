# -*- coding: utf-8 -*-
"""
用户自定义"单词 → 音素"词典管理模块。

【设计变更说明】
不再局限于固定的两个来源（"synthesizerv" / "vocaloid"）。现在支持任意数量的
"独立词典"（每个词典有自己的名字），每个词典在创建时选择一个"记号"
（notation）用于提示该词典里的音素字符串应遵循哪套标记规则：
  - "synthesizerv"：ARPABET 记号（与 word_to_arpabet() / SVP phonemes 字段一致，
                    小写、无重音数字，例如 "hh ah l ow"）
  - "vocaloid"    ：VOCALOID4 音素记号（与 arpabet_to_vocaloid4() 的输出一致，
                    例如 "h @ l ou"）
notation 仅用于 UI 提示与默认值，不限制实际写入的音素字符串内容——用户可以在
任意 notation 的词典里写任何记号，处理流程按用户选择的词典原样使用，不做转换。

仍不支持 OpenUTAU。

词典条目在词典之间不自动互转——用户在词典页面选择要维护的词典后，
录入的音素字符串会被原样存储、原样使用。若调用方传入的词典名不存在
（或是哨兵值 "default"，表示"不使用自定义词典"），会安全地返回 None /
空结果，调用方据此回退到软件默认转换流程（MFA 词典 / g2p_en /
arpabet_to_vocaloid4），不会报错。

【单词大小写】
单词按用户输入的原始大小写存储与显示（不再强制转成全大写），这样才能
在词典管理页面输入/编辑小写单词。但"是否是同一个单词"这件事仍然按
大小写不敏感判断——因为 lookup_word() 在实际处理流程里要拿歌词原文中
的单词（大小写五花八门：句首大写、全大写、全小写……）去匹配词典，
不应该因为大小写不同而错过命中。因此：
  - lookup_word() 精确匹配优先，未命中时退化为大小写不敏感匹配；
  - upsert_entry() / bulk_import() 新增词条时，若词典里已存在仅大小写
    不同的同一单词，会就地替换为新的大小写写法（视为"改写"而不是新增
    一条独立词条），避免出现 hello / HELLO 两条并存、lookup 命中结果
    不确定的情况；
  - delete_entry() 按精确大小写匹配（细节见函数内注释：这是为了配合
    前端"改大小写=先 upsert 新写法再 delete 旧写法"的两步流程，避免
    第二步的删除把刚刚由 upsert 写入的新词条也一并删掉）。

数据以单个 JSON 文件持久化，进程内用一把锁保护，并做了一层内存缓存，
避免每次查词都读盘。

【向后兼容迁移】
本模块的上一版本把数据存成 {"synthesizerv": {...}, "vocaloid": {...}} 这种
"来源→词条"两层结构。首次加载时若检测到这种旧格式，会自动迁移为新的
{"dictionaries": {name: {"notation":..., "entries": {...}}}} 结构（旧的
synthesizerv/vocaloid 词条各自迁移为一本同名词典），并立即落盘，之后旧字段
不再使用。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
DICT_STORE_PATH = PROJECT_DIR / "dictionary" / "user_dictionary.json"

# notation 仅用于 UI 展示 / 新词典默认提示，不做强制校验限制。
VALID_NOTATIONS: Tuple[str, ...] = ("synthesizerv", "vocaloid")
DEFAULT_NOTATION = "synthesizerv"

# "default" 不是一个真实存储的词典，而是"不使用自定义词典，走软件默认转换
# 流程"的哨兵值，调用方直接传入即可，本模块对它一律返回空结果而不报错。
SOURCE_DEFAULT = "default"

_MAX_NAME_LEN = 60

_lock = threading.RLock()
_cache: Optional[Dict[str, Dict[str, object]]] = None  # {name: {"notation":..., "entries": {...}}}


def _empty_store() -> Dict[str, Dict[str, object]]:
    return {}


def _coerce_entries(raw: object) -> Dict[str, str]:
    """规整词条字典。保留单词的原始大小写（不做 .upper()），
    以便词典管理页面可以显示/编辑用户实际输入的大小写。"""
    if not isinstance(raw, dict):
        return {}
    return {
        str(k).strip(): str(v).strip()
        for k, v in raw.items()
        if str(k).strip() and str(v).strip()
    }


def _find_case_insensitive_key(entries: Dict[str, str], word: str) -> Optional[str]:
    """在 entries 中查找与 word 大小写不敏感匹配的已有 key。

    先尝试精确匹配（最常见、性能最优），未命中时退化为逐个大小写不敏感
    比较。用于 lookup_word（匹配歌词原文任意大小写的单词）以及
    upsert_entry / bulk_import（避免 hello / HELLO 同时并存为两条词条）。
    """
    if word in entries:
        return word
    target = word.upper()
    for k in entries:
        if k.upper() == target:
            return k
    return None


def _load() -> Dict[str, Dict[str, object]]:
    """加载词典（带内存缓存）。持有 _lock 时调用。"""
    global _cache
    if _cache is not None:
        return _cache

    if not DICT_STORE_PATH.exists():
        _cache = _empty_store()
        return _cache

    try:
        raw = json.loads(DICT_STORE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("加载用户词典失败（%s），本次以空词典启动: %s", DICT_STORE_PATH, e)
        _cache = _empty_store()
        return _cache

    store: Dict[str, Dict[str, object]] = {}

    if isinstance(raw, dict) and isinstance(raw.get("dictionaries"), dict):
        # 新格式
        for name, payload in raw["dictionaries"].items():
            name = str(name).strip()
            if not name or not isinstance(payload, dict):
                continue
            notation = payload.get("notation")
            if notation not in VALID_NOTATIONS:
                notation = DEFAULT_NOTATION
            store[name] = {
                "notation": notation,
                "entries": _coerce_entries(payload.get("entries")),
            }
        _cache = store
        return _cache

    if isinstance(raw, dict):
        # 旧格式迁移：{"synthesizerv": {...}, "vocaloid": {...}}
        legacy_names = {"synthesizerv": "SynthesizerV", "vocaloid": "VOCALOID"}
        migrated = False
        for legacy_key, display_name in legacy_names.items():
            entries = _coerce_entries(raw.get(legacy_key))
            if entries:
                store[display_name] = {"notation": legacy_key, "entries": entries}
                migrated = True
        if migrated:
            logger.info("检测到旧版词典存储格式，已自动迁移为独立词典（%s）", ", ".join(store.keys()))
            _cache = store
            _save(store)  # 立即落盘，避免下次仍要重复迁移日志
            return _cache

    _cache = _empty_store()
    return _cache


def _save(store: Dict[str, Dict[str, object]]) -> None:
    """持有 _lock 时调用。写盘后刷新缓存。"""
    global _cache
    DICT_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dictionaries": {
            name: {"notation": v.get("notation", DEFAULT_NOTATION), "entries": v.get("entries", {})}
            for name, v in store.items()
        }
    }
    tmp_path = DICT_STORE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_path.replace(DICT_STORE_PATH)  # 原子替换，避免写到一半崩溃损坏文件
    _cache = store


def _normalize_name(name: str) -> str:
    n = (name or "").strip()
    if not n:
        raise ValueError("词典名称不能为空")
    if len(n) > _MAX_NAME_LEN:
        raise ValueError(f"词典名称过长（最多 {_MAX_NAME_LEN} 个字符）")
    if n.lower() == SOURCE_DEFAULT:
        raise ValueError('词典名称不能为 "default"（该名称为保留的哨兵值）')
    return n


def _normalize_notation(notation: Optional[str]) -> str:
    n = (notation or "").strip().lower()
    return n if n in VALID_NOTATIONS else DEFAULT_NOTATION


# ===================== 词典（容器）级别操作 =====================

def list_dictionaries() -> List[Dict[str, object]]:
    """列出所有独立词典及其元信息，按名称排序。"""
    with _lock:
        store = _load()
        return [
            {"name": name, "notation": v.get("notation", DEFAULT_NOTATION), "count": len(v.get("entries", {}))}
            for name, v in sorted(store.items(), key=lambda kv: kv[0].lower())
        ]


def dictionary_exists(name: str) -> bool:
    if not name or name.strip().lower() == SOURCE_DEFAULT:
        return False
    with _lock:
        return name.strip() in _load()


def create_dictionary(name: str, notation: str = DEFAULT_NOTATION) -> Dict[str, object]:
    n = _normalize_name(name)
    notation = _normalize_notation(notation)
    with _lock:
        store = {k: dict(v, entries=dict(v.get("entries", {}))) for k, v in _load().items()}
        # 大小写不敏感判重：避免出现 "MyDict" / "mydict" 这种仅大小写不同、
        # 实际上会让用户误以为是同一本词典的"重名"词典（下拉框里看起来
        # 像是重复项，选择/维护时容易搞混）。
        existing_key = _find_case_insensitive_key(
            {k: "" for k in store.keys()}, n
        )
        if existing_key is not None:
            raise ValueError(f"词典 {existing_key!r} 已存在（名称不区分大小写）")
        store[n] = {"notation": notation, "entries": {}}
        _save(store)
        return {"name": n, "notation": notation, "count": 0}


def rename_dictionary(old_name: str, new_name: str) -> Dict[str, object]:
    """
    重命名一本已存在的独立词典。

    名称判重按大小写不敏感处理（与 create_dictionary 一致），但允许
    "改成大小写不同的自己"这种情况（例如 "mydict" -> "MyDict"）。
    """
    old_n = (old_name or "").strip()
    new_n = _normalize_name(new_name)
    with _lock:
        store = {k: dict(v, entries=dict(v.get("entries", {}))) for k, v in _load().items()}
        if old_n not in store:
            raise ValueError(f"词典 {old_n!r} 不存在")

        if old_n != new_n:
            existing_key = _find_case_insensitive_key(
                {k: "" for k in store.keys() if k != old_n}, new_n
            )
            if existing_key is not None:
                raise ValueError(f"词典 {existing_key!r} 已存在（名称不区分大小写）")

            store[new_n] = store.pop(old_n)
            _save(store)
        entry = store[new_n]
        return {
            "name": new_n,
            "notation": entry.get("notation", DEFAULT_NOTATION),
            "count": len(entry.get("entries", {})),
        }


def delete_dictionary(name: str) -> bool:
    n = (name or "").strip()
    with _lock:
        store = {k: dict(v, entries=dict(v.get("entries", {}))) for k, v in _load().items()}
        existed = n in store
        if existed:
            del store[n]
            _save(store)
        return existed


def _get_or_create(store: Dict[str, Dict[str, object]], name: str, notation: Optional[str]) -> Dict[str, object]:
    if name not in store:
        store[name] = {"notation": _normalize_notation(notation), "entries": {}}
    return store[name]


# ===================== 词条级别操作 =====================

def lookup_word(word: str, source: str) -> Optional[str]:
    """
    在指定词典中查找单词的音素映射（原始字符串，未 split）。

    source 若是哨兵值 "default"（表示用户选择了"使用软件默认值"）或不存在的
    词典名，直接返回 None，调用方据此回退到软件默认转换流程——这里不对
    未命中的词典名抛异常，因为词典可能已被用户删除，静默回退比报错更安全。
    """
    if not word:
        return None
    src = (source or "").strip()
    if not src or src.lower() == SOURCE_DEFAULT:
        return None
    w = word.strip()
    if not w:
        return None
    with _lock:
        store = _load()
        entry = store.get(src)
        if not entry:
            return None
        entries = entry.get("entries", {})
        # 词典条目按用户输入的原始大小写存储，但歌词原文里的单词大小写
        # 五花八门，因此这里按大小写不敏感匹配（内部会先尝试精确匹配）。
        key = _find_case_insensitive_key(entries, w)
        return entries.get(key) if key is not None else None


def get_notation(name: str) -> Optional[str]:
    """
    返回指定词典的记号（notation，"synthesizerv" 或 "vocaloid"）。

    source 若是哨兵值 "default" 或不存在的词典名，返回 None——调用方据此
    判断"未选择/词典已被删除"，不应再尝试按某个记号消费该词典。

    注意：这里返回的是词典本身的 notation 元数据，不是词典名字符串。
    调用方不应再用 `dict_source == "synthesizerv"` 这种方式判断记号——
    "synthesizerv"/"vocaloid" 只是词典创建时选择的记号类型，词典名本身
    是用户自定义的任意字符串（例如 "你好"、"12"、"SynthesizerV" 等），
    两者不能混用比较，否则用户自建的词典永远不会被命中。
    """
    n = (name or "").strip()
    if not n or n.lower() == SOURCE_DEFAULT:
        return None
    with _lock:
        store = _load()
        entry = store.get(n)
        return entry.get("notation") if entry else None


def list_entries(name: str) -> Dict[str, str]:
    with _lock:
        store = _load()
        entry = store.get((name or "").strip())
        if entry is None:
            raise ValueError(f"词典不存在: {name!r}")
        return dict(entry.get("entries", {}))


def upsert_entry(name: str, word: str, phonemes: str, notation: Optional[str] = None) -> None:
    """新增或更新词条。若词典不存在则自动创建（notation 缺省时使用默认记号）。

    单词按原始大小写存储。若词典里已存在仅大小写不同的同一单词，会替换
    为这次传入的大小写写法（视为"改写"），避免 hello / HELLO 并存。
    """
    n = _normalize_name(name)
    word = (word or "").strip()
    phonemes = (phonemes or "").strip()
    if not word:
        raise ValueError("单词不能为空")
    if not phonemes:
        raise ValueError("音素不能为空")

    with _lock:
        store = {k: dict(v, entries=dict(v.get("entries", {}))) for k, v in _load().items()}
        target = _get_or_create(store, n, notation)
        bucket = target["entries"]
        existing_key = _find_case_insensitive_key(bucket, word)
        if existing_key is not None and existing_key != word:
            del bucket[existing_key]
        bucket[word] = phonemes
        _save(store)


def delete_entry(name: str, word: str) -> bool:
    """按精确大小写删除词条（故意不做大小写不敏感匹配）。

    前端"重命名大小写"走的是 upsert_entry(新大小写) 紧接着
    delete_entry(旧大小写) 这套流程：upsert_entry 已经在内部把旧 key
    换成了新 key（大小写不敏感去重）。如果这里的删除也按大小写不敏感
    匹配，就会把刚刚写入的新 key 一并删掉（因为它和"旧单词"大小写不
    敏感相等），词条又会消失。因此改大小写的"旧词条清理"必须精确匹配：
    改名已经在 upsert 阶段完成，这里若找不到精确匹配的旧 key（说明已经
    被 upsert 处理过），直接安全地判定为"不存在"，不做任何删除。
    """
    n = (name or "").strip()
    word = (word or "").strip()

    with _lock:
        store = {k: dict(v, entries=dict(v.get("entries", {}))) for k, v in _load().items()}
        entries = store.get(n, {}).get("entries", {})
        existed = word in entries
        if existed:
            del entries[word]
            _save(store)
        return existed


def bulk_import(name: str, entries: Dict[str, str], overwrite: bool = True,
                 notation: Optional[str] = None) -> Tuple[int, int]:
    """
    批量导入词条。若词典不存在则自动创建。

    Returns
    -------
    (added, updated)
    """
    n = _normalize_name(name)

    with _lock:
        store = {k: dict(v, entries=dict(v.get("entries", {}))) for k, v in _load().items()}
        target = _get_or_create(store, n, notation)
        bucket = target["entries"]
        added, updated = 0, 0

        for raw_word, raw_phones in (entries or {}).items():
            word = (raw_word or "").strip()
            phones = (raw_phones or "").strip()
            if not word or not phones:
                continue
            # 大小写不敏感去重：若已存在仅大小写不同的同一单词，视为更新
            # 该词条（并采用本次导入的大小写写法），而不是新增一条独立词条。
            existing_key = _find_case_insensitive_key(bucket, word)
            if existing_key is not None:
                if overwrite:
                    if existing_key != word:
                        del bucket[existing_key]
                    bucket[word] = phones
                    updated += 1
            else:
                bucket[word] = phones
                added += 1

        _save(store)
        return added, updated


def export_json(name: Optional[str] = None) -> Dict[str, Dict[str, object]]:
    with _lock:
        store = _load()
        if name:
            entry = store.get(name.strip())
            if entry is None:
                raise ValueError(f"词典不存在: {name!r}")
            return {name: {"notation": entry.get("notation"), "entries": dict(entry.get("entries", {}))}}
        return {
            k: {"notation": v.get("notation"), "entries": dict(v.get("entries", {}))}
            for k, v in store.items()
        }


def export_csv(name: str) -> str:
    entries = list_entries(name)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["word", "phonemes"])
    for word in sorted(entries.keys()):
        writer.writerow([word, entries[word]])
    return buf.getvalue()


def import_csv_text(name: str, csv_text: str, overwrite: bool = True,
                     notation: Optional[str] = None) -> Tuple[int, int]:
    """
    解析 "word,phonemes" 两列 CSV（首行可以是表头 word,phonemes，会被自动跳过）。
    """
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return 0, 0

    start_idx = 0
    header = [c.strip().lower() for c in rows[0][:2]]
    if header == ["word", "phonemes"]:
        start_idx = 1

    entries: Dict[str, str] = {}
    for row in rows[start_idx:]:
        if len(row) < 2:
            continue
        entries[row[0]] = row[1]

    return bulk_import(name, entries, overwrite=overwrite, notation=notation)
