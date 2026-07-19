# -*- coding: utf-8 -*-
"""
文本预处理工具模块（"优化文本"弹窗后端）

设计说明
────────
本模块只负责"纯文本 → 纯文本"的字符串转换，不落盘、不联网、不依赖
MFA / TTS / 对齐 / 词典等其它任何后端模块，供 /api/text/optimize 直接
调用。前端"优化文本"弹窗里的每一个按钮都对应本模块的一个函数：

  智能转换 smart_convert()        —— 数字按完整数值读法转换（123→一百
                                      二十三）+ 年份/日期识别（逐字转换，
                                      日期各字段各自按完整数值读）+ 百分号
                                      +摄氏度/华氏度+四则运算符号等，一次
                                      性全部转换。
  仅转换（数字）number_only_convert() —— 只做数字转换（完整数值读法，
                                      不识别日期/年份的特殊拆分，纯粹把
                                      文本中每一段连续数字整体按数值转换），
                                      不动符号。
  逐字转换（数字）digit_to_words_convert() —— 把数字按位逐字转换（不做
                                      数值分组），如 1234567890 → 一二三
                                      四五六七八九零 / one two three ...
                                      zero，适合电话号码/编号/证件号等
                                      场景；与 number_only_convert() 的
                                      "完整数值读法"是两套独立规则，二选
                                      一或先后叠加均可。
  仅转换符号 symbol_only_convert() —— 只转换 + - × ÷ = ℃ ℉ & * # 这组符号，
                                      不动数字（其中 * # 按各语种日常读法
                                      转换，如中文"星号""井号"，不是数学
                                      运算符读法）。
  优化文本 - 英文加空格 add_spaces_around_english() —— 在英文单词（连续
                                      [A-Za-z] 片段）前后补空格，避免中英
                                      文/数字紧贴导致 TTS 分词或强制对齐
                                      出错。
  优化文本 - 去除多余符号 strip_stray_symbols() —— 去掉 "*" 等干扰强制
                                      对齐/TTS 合成、但本身不构成任何读法
                                      的符号。
  优化文本 - 按逗号插入换行 newline_after_comma() —— 中/英文逗号（，,）
                                      每一个之后插入换行，逗号本身保留。
  优化文本 - 按句号插入换行 newline_after_period() —— 中/英文句号/感叹
                                      号/问号（。！？.!?）每一个之后插入
                                      换行，标点本身保留。
  优化文本 - 按每几句插入换行 newline_every_n_sentences() —— 以断句标点
                                      （逗号，,、句号。！？.!? 均算一句）
                                      为分句依据，每凑够 N 句换行一次，N
                                      由前端弹窗的数字输入框传入（默认 2）。

与 pipeline.py 里 _convert_digits_to_words() 的关系
────────────────────────────────────────────────────
pipeline.py 的 _convert_digits_to_words() 是"逐字转换"（1234 → 一二三
四），服务于对齐前的自动预处理，覆盖电话号码/编号等场景，那部分逻辑
保持不动、不受本模块影响。

本模块是给用户在"优化文本"弹窗里手动、可控地编辑参考文本用的，转换
规则是"整段数字按完整数值读"（123 → 一百二十三），二者刻意不同——本
模块的转换结果只会写回文本框里的文字本身，用户确认无误后，这段文字
才会继续走已有的处理流程（包括 pipeline.py 的逐字转换，如果该数字残留
是数字的话；但由于本模块已经把数字转换成汉字/文字，_convert_digits_to_words
就不会再对这段文字做二次处理）。

语言范围
────────
支持 中文（含粤语，二者统一按中文规则处理，书面数字读法本身不区分
简体/繁体/粤语）、英语、日语、韩语。language 参数接受 MFAProcessor.vue /
DialogueBatch.vue 下拉框里实际使用的语言代码（cmn/yue/eng/jpn/kor），
也兼容简写（zh/en/ja/ko）。
"""
from __future__ import annotations

import re
from typing import Dict, List

# ═════════════════════════════════════════════════════════════════════════
# 语言代码归一化：粤语与普通话统一按中文处理
# ═════════════════════════════════════════════════════════════════════════

_LANG_ALIASES: Dict[str, str] = {
    "cmn": "zh", "zh": "zh", "zh-cn": "zh", "zh-tw": "zh", "zh-hk": "zh",
    "yue": "zh",  # 粤语与普通话统一为中文
    "eng": "en", "en": "en", "en-us": "en", "en-gb": "en",
    "jpn": "ja", "ja": "ja",
    "kor": "ko", "ko": "ko",
}


def _norm_lang(language: str) -> str:
    """把前端传来的语言代码（cmn/yue/eng/jpn/kor 等）归一化为
    zh/en/ja/ko 四选一；无法识别时默认按中文处理（与本项目其它模块的
    兜底习惯一致）。"""
    key = (language or "").strip().lower()
    return _LANG_ALIASES.get(key, "zh")


# ═════════════════════════════════════════════════════════════════════════
# 整数 → 完整数值读法文字（123 → 一百二十三 / one hundred twenty-three /
# 百二十三 / 백이십삼）
#   与 pipeline.py 的"逐字转换"（1234→一二三四）是两套完全独立的规则，
#   服务于不同场景，互不调用、互不影响。
# ═════════════════════════════════════════════════════════════════════════

# ── 中文（含粤语） ──────────────────────────────────────────────────────
_ZH_DIGITS = "零一二三四五六七八九"
_ZH_UNITS_SMALL = ["", "十", "百", "千"]
_ZH_UNITS_BIG = ["", "万", "亿", "兆"]


def _zh_group(g: int, is_highest_group: bool) -> str:
    """把 0-9999 转成中文（不含万/亿等大单位）。is_highest_group 为 True
    时，组内最高位若为"十位=1"（即十几），按中文习惯省略"一"（十五而非
    一十五）；非最高分组时不能省略（一万一十五不能写成一万十五）。"""
    if g == 0:
        return ""
    digits = [int(c) for c in str(g).zfill(4)]
    part = ""
    leading = True
    zero_pending = False
    for i, d in enumerate(digits):
        unit = _ZH_UNITS_SMALL[3 - i]
        if d == 0:
            if part:
                zero_pending = True
            continue
        if zero_pending:
            part += "零"
            zero_pending = False
        if d == 1 and unit == "十" and leading and is_highest_group:
            part += "十"
        else:
            part += _ZH_DIGITS[d] + unit
        leading = False
    return part


def int_to_zh(num: int) -> str:
    """整数 → 中文完整数值读法。支持到"兆"级（万亿），超出范围极少见，
    本工具面向歌词/台词文本，实际输入几乎不会超过这个量级。"""
    if num == 0:
        return "零"
    neg = num < 0
    num = abs(num)
    s = str(num)
    groups: List[str] = []
    while s:
        groups.insert(0, s[-4:])
        s = s[:-4]

    group_cn: List[str] = []
    for gi, g in enumerate(groups):
        gval = int(g)
        is_highest_group = (gi == 0)
        part = _zh_group(gval, is_highest_group)
        big_unit = _ZH_UNITS_BIG[len(groups) - 1 - gi] if gval else ""
        group_cn.append(part + big_unit)

    result = ""
    for gi, g in enumerate(groups):
        gval = int(g)
        if gval == 0:
            # 组间补零：该组为 0 但后面（含自身之后）仍有非零组时，需要
            # 补一个"零"占位（10001 → 一万零一），已有"零"结尾则不重复补。
            if result and any(int(x) != 0 for x in groups[gi:]):
                if not result.endswith("零"):
                    result += "零"
            continue
        if result and gval < 1000 and not result.endswith("零"):
            result += "零"
        result += group_cn[gi]
    return ("负" if neg else "") + result


# ── 英语 ────────────────────────────────────────────────────────────────
_EN_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen",
]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_EN_BIG = ["", "thousand", "million", "billion", "trillion"]


def _en_three_digits(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(_EN_ONES[n // 100] + " hundred")
        n %= 100
    if n >= 20:
        t = _EN_TENS[n // 10]
        if n % 10:
            t += "-" + _EN_ONES[n % 10]
        parts.append(t)
    elif n > 0:
        parts.append(_EN_ONES[n])
    return " ".join(parts)


def int_to_en(num: int) -> str:
    """整数 → 英文基数词读法（美式习惯，不插入 "and"：123 → one hundred
    twenty-three）。"""
    if num == 0:
        return "zero"
    neg = num < 0
    num = abs(num)
    groups: List[int] = []
    n = num
    while n > 0:
        groups.append(n % 1000)
        n //= 1000
    parts = []
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g == 0:
            continue
        seg = _en_three_digits(g)
        if i < len(_EN_BIG) and _EN_BIG[i]:
            seg += " " + _EN_BIG[i]
        parts.append(seg)
    return ("negative " if neg else "") + " ".join(parts)


# ── 日语（假名读法） ────────────────────────────────────────────────────
# 本工具的输出会直接喂给强制对齐 / TTS 流程，日语必须落地成平假名（表音），
# 汉数字（百二十三）虽然书面上常见，但不是可直接拼读的音素序列，会导致
# 对齐/合成读错。因此这里给出的不是"数字→汉数字"的书面转写，而是
# "数字→假名读音"的完整音读展开。
_JA_DIGIT_KANA = ["ゼロ", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう"]
# 十/百/千 与个位数结合时的读音变化（促音便/连浊等），按标准数词读法表列出
_JA_JUU_KANA = ["", "じゅう", "にじゅう", "さんじゅう", "よんじゅう", "ごじゅう",
                "ろくじゅう", "ななじゅう", "はちじゅう", "きゅうじゅう"]
_JA_HYAKU_KANA = ["", "ひゃく", "にひゃく", "さんびゃく", "よんひゃく", "ごひゃく",
                  "ろっぴゃく", "ななひゃく", "はっぴゃく", "きゅうひゃく"]
_JA_SEN_KANA = ["", "せん", "にせん", "さんぜん", "よんせん", "ごせん",
                "ろくせん", "ななせん", "はっせん", "きゅうせん"]
_JA_BIG_KANA = ["", "まん", "おく", "ちょう"]


def _ja_group_kana(g: int) -> str:
    """0-9999 の 4 桁分を仮名読みに変換する。"""
    if g == 0:
        return ""
    sen = g // 1000
    hyaku = (g % 1000) // 100
    juu = (g % 100) // 10
    ichi = g % 10
    part = _JA_SEN_KANA[sen] + _JA_HYAKU_KANA[hyaku] + _JA_JUU_KANA[juu]
    if ichi:
        part += _JA_DIGIT_KANA[ichi]
    return part


def int_to_ja(num: int) -> str:
    """整数 → 日语平假名读法（123 → ひゃくにじゅうさん）。用于喂给 TTS /
    强制对齐，必须是可直接拼读的表音形式，因此不用汉数字。"""
    if num == 0:
        return _JA_DIGIT_KANA[0]
    neg = num < 0
    num = abs(num)
    groups: List[int] = []
    n = num
    while n > 0:
        groups.append(n % 10000)
        n //= 10000
    parts = []
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g == 0:
            continue
        if i > 0 and g == 1:
            # 万/億/兆 位为 1 时仍读作"いち"+位（いちまん），与十/百/千不同，
            # 那三个 1 是隐去的（じゅう而非いちじゅう）。
            seg = _JA_DIGIT_KANA[1]
        else:
            seg = _ja_group_kana(g)
        big = _JA_BIG_KANA[i] if i < len(_JA_BIG_KANA) else ""
        parts.append(seg + big)
    return ("マイナス" if neg else "") + "".join(parts)


# ── 韩语（汉字数词，用于日期/数字/编号场景，与固有数词是两套体系，本工具
#    固定使用汉字数词，因为它是书面数字/日期最通用的读法） ────────────────
_KO_DIGITS = "영일이삼사오육칠팔구"
_KO_SMALL = ["", "십", "백", "천"]
_KO_BIG = ["", "만", "억", "조"]


def _ko_group(g: int) -> str:
    if g == 0:
        return ""
    digits = [int(c) for c in str(g).zfill(4)]
    part = ""
    for i, d in enumerate(digits):
        unit = _KO_SMALL[3 - i]
        if d == 0:
            continue
        if d == 1 and unit:
            part += unit
        else:
            part += _KO_DIGITS[d] + unit
    return part


def int_to_ko(num: int) -> str:
    """整数 → 韩语汉字数词读法（123 → 백이십삼；만/억/조 位为 1 时保留
    "일"：10000 → 일만）。"""
    if num == 0:
        return "영"
    neg = num < 0
    num = abs(num)
    groups: List[int] = []
    n = num
    while n > 0:
        groups.append(n % 10000)
        n //= 10000
    parts = []
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g == 0:
            continue
        seg = _ko_group(g)
        big = _KO_BIG[i] if i < len(_KO_BIG) else ""
        if i > 0 and g == 1:
            seg = "일"
        parts.append(seg + big)
    return ("마이너스" if neg else "") + "".join(parts)


_INT_TO_WORDS = {
    "zh": int_to_zh,
    "en": int_to_en,
    "ja": int_to_ja,
    "ko": int_to_ko,
}


# ═════════════════════════════════════════════════════════════════════════
# 对外入口（新增）：逐字转换（数字，按位读，不做数值分组）
#   1234567890 → 一二三四五六七八九零（中/粤，无分隔符）
#               → one two three four five six seven eight nine zero（英）
#               → いち に さん よん ご ろく なな はち きゅう ぜろ（日）
#               → 일 이 삼 사 오 육 칠 팔 구 영（韩）
#   与"仅转换（数字，完整数值读法）"number_only_convert() 是两套完全独立
#   的规则，服务于不同场景（电话号码/编号/证件号等适合逐字读，金额/年龄/
#   数量等适合完整数值读），互不调用、互不影响，在"优化文本"弹窗里是并列
#   的两个按钮，可任选其一或先后叠加使用。
# ═════════════════════════════════════════════════════════════════════════

_ZH_DIGIT_WORDS = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
_EN_DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_JA_DIGIT_WORDS = ["ぜろ", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう"]
_KO_DIGIT_WORDS = ["영", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구"]

_DIGIT_WORDS_TABLE: Dict[str, List[str]] = {
    "zh": _ZH_DIGIT_WORDS,
    "en": _EN_DIGIT_WORDS,
    "ja": _JA_DIGIT_WORDS,
    "ko": _KO_DIGIT_WORDS,
}

# 中/日/韩逐字转换后紧贴书写（不加分隔符，与书面数字习惯一致，如手机号
# 一二三四五六七八九零一一）；英语则每个词之间需要一个空格分隔，否则会
# 变成一长串无法断词的字母（onetwothree...），这与产品需求样例
# "one two three four five six seven eight nine zero" 完全一致。
_DIGIT_WORDS_SEP: Dict[str, str] = {"zh": "", "en": " ", "ja": " ", "ko": " "}


def digit_to_words_convert(text: str, language: str) -> str:
    """把 text 里所有阿拉伯数字 0-9 按 language 对应的语种转换成读法文字。
    转换规则（与产品需求给出的样例完全一致）：
      中文/粤语 1234567890 → 一二三四五六七八九零（无分隔符）
      英语      1234567890 → one two three four five six seven eight nine zero
      日语      1234567890 → いち に さん よん ご ろく なな はち きゅう ぜろ
      韩语      1234567890 → 일 이 삼 사 오 육 칠 팔 구 영
    text 中非数字部分原样保留；language 经 _norm_lang() 规整后若不在
    上表中（暂不支持该语种的数字转换），原样返回 text，不做任何改动。
    text 中本就不含数字时直接返回原文本，不做无意义的字符串重建。
    """
    if not text or not any(ch.isdigit() for ch in text):
        return text
    lang = _norm_lang(language)
    words = _DIGIT_WORDS_TABLE.get(lang)
    if words is None:
        return text
    sep = _DIGIT_WORDS_SEP.get(lang, "")

    out: List[str] = []
    run: List[str] = []

    def _flush() -> None:
        if run:
            out.append(sep.join(run))
            run.clear()

    for ch in text:
        if ch.isdigit():
            run.append(words[int(ch)])
        else:
            _flush()
            out.append(ch)
    _flush()
    return "".join(out)


def _number_to_words(num_str: str, lang: str) -> str:
    """把一串纯数字文本（可能含前导零，如编号 "007"）转成完整数值读法。
    含小数点的数字（如 "3.14"）整数部分和小数部分分别转换、用"点/point/
    てん/점"连接；小数部分逐位读（与整数部分的完整数值读法不同，因为小数
    没有"十百千"的概念，3.14 应读作"三点一四"而不是"三点十四"）。"""
    convert = _INT_TO_WORDS.get(lang, int_to_zh)
    if "." in num_str:
        int_part, _, frac_part = num_str.partition(".")
        int_part = int_part or "0"
        int_words = convert(int(int_part))
        frac_digit_words = {
            "zh": "零一二三四五六七八九",
            "en": ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"],
            "ja": _JA_DIGIT_KANA,
            "ko": "영일이삼사오육칠팔구",
        }
        sep = {"zh": "点", "en": " point ", "ja": "てん", "ko": "점"}
        digits_table = frac_digit_words.get(lang, frac_digit_words["zh"])
        if lang in ("en", "ja"):
            frac_words = " ".join(digits_table[int(d)] for d in frac_part) if lang == "en" \
                else "".join(digits_table[int(d)] for d in frac_part)
        else:
            frac_words = "".join(digits_table[int(d)] for d in frac_part)
        return f"{int_words}{sep.get(lang, '点')}{frac_words}"
    return convert(int(num_str))


# ═════════════════════════════════════════════════════════════════════════
# 符号 → 各语种读法文字
#   "+ - × ÷ = ℃ ℉ &" 对应 "加 减 乘 除 等于 摄氏度 华氏度 和"（中文示例，
#   其余语种同理）。
# ═════════════════════════════════════════════════════════════════════════

_SYMBOL_WORDS: Dict[str, Dict[str, str]] = {
    "zh": {
        "+": "加", "-": "减", "×": "乘", "÷": "除", "=": "等于",
        "℃": "摄氏度", "℉": "华氏度", "&": "和",
        "*": "星号",  # 键盘常见输入，日常读法是"星号"而非数学乘号
        "#": "井号",
        "%": "百分之",  # 百分号单独处理（见 _convert_percent），此处仅在
                         # "仅转换符号"模式下若单独出现也能兜底转换
    },
    "en": {
        "+": "plus", "-": "minus", "×": "times", "÷": "divided by", "=": "equals",
        "℃": "degrees Celsius", "℉": "degrees Fahrenheit", "&": "and",
        "*": "asterisk",
        "#": "hash",
        "%": "percent",
    },
    "ja": {
        "+": "プラス", "-": "マイナス", "×": "かける", "÷": "わる", "=": "イコール",
        "℃": "度", "℉": "華氏度", "&": "アンド",
        "*": "アスタリスク",
        "#": "シャープ",
        "%": "パーセント",
    },
    "ko": {
        "+": "더하기", "-": "빼기", "×": "곱하기", "÷": "나누기", "=": "같다",
        "℃": "섭씨 도", "℉": "화씨 도", "&": "그리고",
        "*": "별표",
        "#": "샵",
        "%": "퍼센트",
    },
}

# 符号转换正则：不含 %（百分号走 _convert_percent 的数字上下文识别，避免
# "仅转换符号"把孤立的 % 也转换掉、和百分比规则重复处理）。
#
# "-" 单独处理：普通的 + × ÷ = ℃ ℉ & * 不会出现在任何语言的正常单词内部，
# 直接整体匹配替换是安全的；但 "-" 会出现在英文连字符复合词内部（如数字
# 转换产出的 "twenty-one"、原文里可能出现的 "well-known"），如果不加区分
# 地整体替换会把这些词从中间拆开、插入 "minus" 破坏原词。这里用负向环视
# 限定："-" 两侧只要有任意一侧不是字母（数字/空格/标点/文本边界），才当作
# 独立的减号符号转换；两侧都是字母时视为单词内部连字符，不转换。
def _symbol_pattern_for(lang: str) -> "re.Pattern":
    table = _SYMBOL_WORDS.get(lang, _SYMBOL_WORDS["zh"])
    other_chars = [k for k in table.keys() if k not in ("%", "-")]
    escaped_others = "|".join(re.escape(c) for c in sorted(other_chars, key=len, reverse=True))
    parts = []
    if "-" in table:
        parts.append(r"(?<![A-Za-z])-(?![A-Za-z])")
    if escaped_others:
        parts.append(f"(?:{escaped_others})")
    return re.compile("|".join(parts))


def symbol_only_convert(text: str, language: str) -> str:
    """仅转换符号：把 + - × ÷ = ℃ ℉ & 按当前语种转换成对应文字，不触碰
    文本中的数字部分。英语符号词两侧补空格（three plus five，与英语单词
    间必须有空格的书写习惯一致）；中/日/韩符号词不加空格（书写习惯本身
    不依赖空格，逐字直接拼接）。"""
    if not text:
        return text
    lang = _norm_lang(language)
    table = _SYMBOL_WORDS.get(lang, _SYMBOL_WORDS["zh"])
    pattern = _symbol_pattern_for(lang)
    if lang == "en":
        result = pattern.sub(lambda m: f" {table[m.group(0)]} ", text)
        lines = result.split("\n")
        return "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in lines)
    return pattern.sub(lambda m: table[m.group(0)], text)


# ═════════════════════════════════════════════════════════════════════════
# 百分比：1% → 百分之一 / one percent / 一パーセント / 일 퍼센트
#   与"仅转换符号"里孤立 % 的兜底转换（_SYMBOL_WORDS 里的 "%": "百分之"）
#   是两套入口：这里是"数字+% "整体识别、语序按各语言习惯调整（中文/日语/
#   韩语"百分之"在数字前，英语"percent"在数字后）；符号表里的兜底只在
#   "仅转换符号"模式下、且 % 前面不是数字时才可能被用到（几乎不会触发，
#   保留只是为了任何孤立 % 输入都有兜底文字，不会被无声吞掉）。
# ═════════════════════════════════════════════════════════════════════════

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _convert_percent(text: str, lang: str) -> str:
    def _replace(m: "re.Match") -> str:
        num_words = _number_to_words(m.group(1), lang)
        if lang == "en":
            return f"{num_words} percent"
        if lang == "ja":
            return f"{num_words}パーセント"
        if lang == "ko":
            return f"{num_words} 퍼센트"
        return f"百分之{num_words}"

    return _PERCENT_RE.sub(_replace, text)


# ═════════════════════════════════════════════════════════════════════════
# 温度符号：20℃ → 二十摄氏度 / twenty degrees Celsius / 二十度 / 섭씨 이십 도
#   数字 + ℃/℉ 整体识别，与"仅转换符号"里孤立 ℃/℉ 的兜底转换是两套入口，
#   逻辑关系与百分比完全一致（见上面的说明）。
# ═════════════════════════════════════════════════════════════════════════

_TEMP_RE = re.compile(r"(?<![\d)])(-?\d+(?:\.\d+)?)\s*(℃|℉)")


def _convert_temperature(text: str, lang: str) -> str:
    def _replace(m: "re.Match") -> str:
        raw_num, unit = m.group(1), m.group(2)
        neg = raw_num.startswith("-")
        num_words = _number_to_words(raw_num.lstrip("-"), lang)
        if lang == "en":
            unit_word = "degrees Celsius" if unit == "℃" else "degrees Fahrenheit"
            prefix = "negative " if neg else ""
            return f"{prefix}{num_words} {unit_word}"
        if lang == "ja":
            unit_word = "度" if unit == "℃" else "華氏度"
            prefix = "マイナス" if neg else ""
            return f"{prefix}{num_words}{unit_word}"
        if lang == "ko":
            unit_word = "섭씨" if unit == "℃" else "화씨"
            prefix = "마이너스 " if neg else ""
            return f"{prefix}{unit_word} {num_words} 도"
        # 中文（含粤语）
        unit_word = "摄氏度" if unit == "℃" else "华氏度"
        prefix = "负" if neg else ""
        return f"{prefix}{num_words}{unit_word}"

    return _TEMP_RE.sub(_replace, text)


# ═════════════════════════════════════════════════════════════════════════
# 日期 / 年份识别
#   年份：逐字转换（2020 → 二零二零，与口语年份读法一致）。
#   日期：年/月/日各字段独立识别——年份逐字转换，月和日按完整数值读法转换
#   （12月21日 → 十二月二十一日）。仅支持中文场景常见的 "YYYY年MM月DD日"
#   / "YYYY/MM/DD" / "YYYY-MM-DD" 书写，及英文 "MM/DD/YYYY" 等常见记法；
#   若文本不满足这些结构化模式，日期部分不做特殊处理，交由后续的普通数字
#   转换规则（完整数值读法）兜底处理。
# ═════════════════════════════════════════════════════════════════════════

_ZH_YEAR_DIGIT = "零一二三四五六七八九"
_EN_YEAR_DIGIT = ["oh", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_JA_YEAR_DIGIT = _JA_DIGIT_KANA  # 平假名逐位读（ゼロ/いち/に…），与其它数字读法保持表音一致
_KO_YEAR_DIGIT = "영일이삼사오육칠팔구"


def _year_to_words(year_str: str, lang: str) -> str:
    """年份逐字转换（书写形式）：2020 → 二〇二〇。英语年份口语上常读作
    "twenty twenty"，但那是朗读规则，本工具做的是书面转换，书面上英文年
    份并无统一的"分组数字词"写法，因此英文年份改为整体按完整数值读出
    （2020 → two thousand twenty），与英文日期书写习惯一致。"""
    if lang == "en":
        return int_to_en(int(year_str))
    table = {"zh": _ZH_YEAR_DIGIT, "ja": _JA_YEAR_DIGIT, "ko": _KO_YEAR_DIGIT}.get(lang, _ZH_YEAR_DIGIT)
    return "".join(table[int(d)] for d in year_str)


# 中文/日语 "YYYY年MM月DD日"（月/日部分可选，年份必须存在；MM/DD 允许 1-2 位）
_CJK_DATE_RE = re.compile(
    r"(?P<year>\d{4})年(?:(?P<month>\d{1,2})月)?(?:(?P<day>\d{1,2})日)?"
)
# 中文/日语 单独 "YYYY年"（不含月日，避免被 _CJK_DATE_RE 处理后已替换的部分
# 二次匹配——两者用同一个 pattern 的可选组已经覆盖，这里不需要单独的正则）

# 数字型日期："YYYY/MM/DD"、"YYYY-MM-DD"（年在前，ISO 习惯，中日韩常见）
_ISO_DATE_RE = re.compile(r"\b(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\b")

# 韩语 "YYYY년MM월DD일"
_KO_DATE_RE = re.compile(
    r"(?P<year>\d{4})년(?:\s*(?P<month>\d{1,2})월)?(?:\s*(?P<day>\d{1,2})일)?"
)


def _convert_dates_and_years(text: str, lang: str) -> str:
    """识别并转换文本中的年份/日期结构，返回替换后的文本。识别不到结构化
    日期时不做任何改动，留给后续的普通数字转换规则处理。"""

    if lang == "ko":
        def _ko_date_replace(m: "re.Match") -> str:
            year, month, day = m.group("year"), m.group("month"), m.group("day")
            out = _year_to_words(year, "ko") + "년"
            if month:
                out += int_to_ko(int(month)) + "월"
            if day:
                out += int_to_ko(int(day)) + "일"
            return out
        text = _KO_DATE_RE.sub(_ko_date_replace, text)
    elif lang in ("zh", "ja"):
        unit_month = "月"
        unit_day = "日"
        convert = int_to_zh if lang == "zh" else int_to_ja

        def _cjk_date_replace(m: "re.Match") -> str:
            year, month, day = m.group("year"), m.group("month"), m.group("day")
            out = _year_to_words(year, lang) + "年"
            if month:
                out += convert(int(month)) + unit_month
            if day:
                out += convert(int(day)) + unit_day
            return out
        text = _CJK_DATE_RE.sub(_cjk_date_replace, text)

    # ISO 数字型日期（YYYY/MM/DD、YYYY-MM-DD）：中/日/韩已经在结构化模式
    # 里被年/月/日汉字包裹处理，这里主要覆盖英文场景（美式 December
    # twenty-first twenty twenty 这类完整口语化转换涉及月历名，超出"书面
    # 数字转换"范畴，容易与地区习惯冲突，因此英文 ISO 日期仅做数字→数值
    # 读法转换，年/月/日之间保留原分隔符，不额外拼装月历名，交给使用者
    # 自行判断是否需要进一步整理）。
    if lang == "en":
        def _iso_date_replace(m: "re.Match") -> str:
            year, month, day = m.group(1), m.group(2), m.group(3)
            return f"{int_to_en(int(year))} {int_to_en(int(month))} {int_to_en(int(day))}"
        text = _ISO_DATE_RE.sub(_iso_date_replace, text)
    else:
        def _iso_date_replace_cjk(m: "re.Match") -> str:
            year, month, day = m.group(1), m.group(2), m.group(3)
            convert = int_to_zh if lang != "ja" else int_to_ja
            return f"{_year_to_words(year, lang)}年{convert(int(month))}月{convert(int(day))}日"
        text = _ISO_DATE_RE.sub(_iso_date_replace_cjk, text)

    return text


# ═════════════════════════════════════════════════════════════════════════
# 分数："分子/分母" → 各语种分数读法
#   中文：三分之一（先读分母再读分子，"分母分之分子"）
#   英文：one third（分子基数词 + 分母序数词，分子≠1 时序数词加 s：
#         two thirds）
#   日语：三分の一（同中文语序，先分母后分子，用平假名"ぶんの"连接，与本
#         工具其它日语输出保持表音一致，不用汉字"分の"）
#   韩语：삼분의 일（先分母后分子，"분의"连接）
#   仅识别形如 "分子/分母" 的最简整数分数写法（如 1/3、2/5），不含空格，
#   分子分母都是纯数字；避免误吞日期用的 YYYY/MM/DD（那里分子分母段落更
#   长且通常有 4 位年份，由 _convert_dates_and_years 先处理并替换掉）。
# ═════════════════════════════════════════════════════════════════════════

_FRACTION_RE = re.compile(r"(?<![\d/])(-?\d+)/(\d+)(?![\d/])")

_EN_ORDINAL_SMALL = {
    2: "half", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh",
    8: "eighth", 9: "ninth", 10: "tenth",
}


def _en_ordinal(n: int) -> str:
    """英文分母的序数词读法：2-10 用固定的不规则/规则词表（half/third/...），
    更大的分母走"基数词+th"通用规则（如 11 → eleventh 这类不规则的没有
    覆盖到，但作为分数场景已经足够常见，超出部分留给使用者自行校对）。"""
    if n in _EN_ORDINAL_SMALL:
        return _EN_ORDINAL_SMALL[n]
    base = int_to_en(n)
    return base + "th"


def _convert_fractions(text: str, lang: str) -> str:
    def _replace(m: "re.Match") -> str:
        num_raw, den_raw = m.group(1), m.group(2)
        neg = num_raw.startswith("-")
        num = int(num_raw.lstrip("-"))
        den = int(den_raw)
        if den == 0:
            return m.group(0)  # 分母为 0 非法分数，原样保留

        if lang == "en":
            ordinal = _en_ordinal(den)
            if ordinal != "half" and num != 1:
                ordinal += "s"
            words = f"{int_to_en(num)} {ordinal}"
            return f"negative {words}" if neg else words

        if lang == "ja":
            words = f"{int_to_ja(den)}ぶんの{int_to_ja(num)}"
            return f"マイナス{words}" if neg else words

        if lang == "ko":
            words = f"{int_to_ko(den)}분의 {int_to_ko(num)}"
            return f"마이너스 {words}" if neg else words

        # 中文（含粤语）
        words = f"{int_to_zh(den)}分之{int_to_zh(num)}"
        return f"负{words}" if neg else words

    return _FRACTION_RE.sub(_replace, text)


# ═════════════════════════════════════════════════════════════════════════
# 普通数字（完整数值读法）：123 → 一百二十三
#   在日期/年份/百分比/温度都处理完之后，对文本里剩余的纯数字串统一按
#   完整数值读法转换。放在最后执行，避免把已经识别成年份/日期一部分的
#   数字重复转换。
# ═════════════════════════════════════════════════════════════════════════

_PLAIN_NUMBER_RE = re.compile(r"(?<![\d)])-\d+(?:\.\d+)?|\d+(?:\.\d+)?")


def _convert_plain_numbers(text: str, lang: str) -> str:
    if not text or not any(ch.isdigit() for ch in text):
        return text

    def _replace(m: "re.Match") -> str:
        raw = m.group(0)
        neg = raw.startswith("-")
        core = raw.lstrip("-")
        words = _number_to_words(core, lang)
        if neg:
            prefix = {"zh": "负", "en": "negative ", "ja": "マイナス", "ko": "마이너스 "}.get(lang, "负")
            return prefix + words
        return words

    return _PLAIN_NUMBER_RE.sub(_replace, text)


# ═════════════════════════════════════════════════════════════════════════
# 对外入口 1：智能转换（数字 + 日期/年份 + 百分比 + 温度 + 四则运算符号
#   一次性全部转换）
# ═════════════════════════════════════════════════════════════════════════

def smart_convert(text: str, language: str) -> str:
    """智能转换：按以下优先级依次处理：
      1. 年份 / 日期（2020年12月21日 → 二〇二〇年十二月二十一日）
      2. 分数（1/3 → 三分之一 / one third / 三分の一 / 삼분의 일）
      3. 百分比（1% → 百分之一）
      4. 温度（20℃ → 二十摄氏度，68℉ → 六十八华氏度）
      5. 独立的减号/连字符符号（作为数学运算符使用、不是数字自带负号的
         那些 "-"）先替换成一个不含字母/数字的占位符，避免第 6 步把英文
         数字转换成带连字符的复合词（如 "twenty-one"）后，与本来就是减号
         的 "-" 混在一起分不清谁是谁。
      6. 剩余的普通数字（123 → 一百二十三；-5 → negative five，数字自带
         的负号在这一步被识别消费掉，不会误当成减号占位符处理，因为它在
         第 5 步就没有被替换——第 5 步的正则本来就只匹配"非负号"的那些
         "-"）。
      7. 占位符替换回目标语种的减号文字；同时处理 + × ÷ = & * # 等其余
         符号（* # 按各语种日常读法转换，如中文"星号""井号"）。
    """
    if not text:
        return text
    lang = _norm_lang(language)

    text = _convert_dates_and_years(text, lang)
    text = _convert_fractions(text, lang)
    text = _convert_percent(text, lang)
    text = _convert_temperature(text, lang)
    text = _protect_minus_as_placeholder(text)
    text = _convert_plain_numbers(text, lang)
    text = _resolve_minus_placeholder(text, lang)
    text = symbol_only_convert(text, lang)
    return text


# 减号占位符：使用一个不会与任何语言的文字/数字/常规标点冲突的私用区字符，
# 在"普通数字转换"阶段之前，把"真正作为运算符使用的减号"替换成占位符，
# 转换完数字之后再统一替换回目标语种的文字。这样即使英文数字转换会产出
# 带连字符的复合词（twenty-one），也不会与占位符（此时已经不是普通的
# "-" 字符本身）产生任何匹配冲突。
#
# 用逐字符扫描而不是单一正则，是因为"这个 '-' 到底是减号还是负号"需要
# 同时看它左右两侧的字符类型，纯正则的环视条件组合容易出错、可读性也
# 差；这里的判定规则与 _PLAIN_NUMBER_RE 对负号的判定完全一致（负号 = 后
# 面紧跟数字，且前面不是数字或右括号），避免两处规则不一致导致"占位符
# 替换"和"数字转换"各按各的理解、结果对不上。
#   负号（不替换，原样保留给数字转换消费）：后面是数字，且前面不是数字/")"
#   单词内连字符（不替换，原样保留）：前后都是英文字母（twenty-one 这类）
#   其余情况的独立 "-"（替换成占位符）：真正的减号运算符
_MINUS_PLACEHOLDER = "\uE000"


def _protect_minus_as_placeholder(text: str) -> str:
    out = []
    n = len(text)
    for i, c in enumerate(text):
        if c != "-":
            out.append(c)
            continue
        prev_ch = text[i - 1] if i > 0 else ""
        next_ch = text[i + 1] if i + 1 < n else ""
        is_negative_sign = next_ch.isdigit() and not (prev_ch.isdigit() or prev_ch == ")")
        is_word_internal = (
            prev_ch.isascii() and prev_ch.isalpha() and next_ch.isascii() and next_ch.isalpha()
        )
        out.append(c if (is_negative_sign or is_word_internal) else _MINUS_PLACEHOLDER)
    return "".join(out)


def _resolve_minus_placeholder(text: str, lang: str) -> str:
    word = _SYMBOL_WORDS.get(lang, _SYMBOL_WORDS["zh"])["-"]
    if lang == "en":
        return text.replace(_MINUS_PLACEHOLDER, f" {word} ")
    return text.replace(_MINUS_PLACEHOLDER, word)


# ═════════════════════════════════════════════════════════════════════════
# 对外入口 2：仅转换（数字，按完整数值读法，如 123 → 一百二十三）
#   不识别日期/年份的特殊拆分（不会把 2020年12月21日 里的年份逐字转换成
#   二零二零，而是整段数字各自按完整数值读），不处理符号，专注"纯数字"。
# ═════════════════════════════════════════════════════════════════════════

def number_only_convert(text: str, language: str) -> str:
    """仅转换（数字）：文本中每一段连续数字整体按完整数值读法转换，不做
    年份/日期的逐字特殊处理，也不转换百分号/摄氏度/华氏度/四则运算符号。"""
    if not text:
        return text
    lang = _norm_lang(language)
    return _convert_plain_numbers(text, lang)


# ═════════════════════════════════════════════════════════════════════════
# 对外入口 3：文本优化 —— 英文单词首尾补空格
#   在连续的英文字母片段（[A-Za-z]+）前后各补一个空格，避免其与中文/
#   数字/标点紧贴导致 TTS 分词、强制对齐或字典查词出错。已经被空格/
#   标点/文本边界包围的英文片段不会被重复加空格。
# ═════════════════════════════════════════════════════════════════════════

_ENGLISH_RUN_RE = re.compile(r"[A-Za-z]+")


def add_spaces_around_english(text: str) -> str:
    """在每一段连续英文字母前后补空格（不区分语种，任意语种文本里混入的
    英文单词都适用这条规则）。"""
    if not text:
        return text

    def _replace(m: "re.Match") -> str:
        return f" {m.group(0)} "

    spaced = _ENGLISH_RUN_RE.sub(_replace, text)
    # 补空格可能在行首/行尾或紧邻已有空格处产生多余空格，这里逐行清理：
    #   - 合并连续空格为一个
    #   - 去掉行首/行尾空格（保留换行结构，不合并跨行空白）
    lines = spaced.split("\n")
    cleaned_lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in lines]
    return "\n".join(cleaned_lines)


# ═════════════════════════════════════════════════════════════════════════
# 对外入口（新增）：大写字母逐个加空格 / 大写转小写 / 小写转大写
#   三个按钮均只处理英文字母（A-Z / a-z），不影响其它任何文字与标点：
#     · 大写字母逐个加空格：每一个大写字母前后补空格，把连写的大写缩写
#       拆成单字母序列（"ABC" → "A B C"），便于 TTS / 强制对齐按字母朗读，
#       常见于处理英文缩写、字母歌一类的场景。已经是小写或非字母字符不受
#       影响；同一行内因补空格产生的多余空格按其它按钮的惯例统一清理。
#     · 大写转小写：整段文本里的英文字母统一转小写（"VOCAL" → "vocal"）。
#     · 小写转大写：整段文本里的英文字母统一转大写（"Vocal" → "VOCAL"）。
#   后两个直接复用 Python 内置 str.upper()/str.lower()，对中日韩等非英文
#   字符没有任何影响，不需要额外处理。
# ═════════════════════════════════════════════════════════════════════════

_UPPERCASE_LETTER_RE = re.compile(r"[A-Z]")


def add_spaces_around_uppercase(text: str) -> str:
    """把每一个大写英文字母前后都补上空格，让连写的大写字母逐个断开
    （如 "ABC" → "A B C"）。不影响小写字母及其它任何文字/标点。"""
    if not text:
        return text

    def _replace(m: "re.Match") -> str:
        return f" {m.group(0)} "

    spaced = _UPPERCASE_LETTER_RE.sub(_replace, text)
    lines = spaced.split("\n")
    cleaned_lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in lines]
    return "\n".join(cleaned_lines)


def uppercase_to_lowercase(text: str) -> str:
    """把文本中的英文字母统一转换为小写（如 "VOCAL" → "vocal"），
    不影响非英文字母字符。"""
    if not text:
        return text
    return text.lower()


def lowercase_to_uppercase(text: str) -> str:
    """把文本中的英文字母统一转换为大写（如 "Vocal" → "VOCAL"），
    不影响非英文字母字符。"""
    if not text:
        return text
    return text.upper()


# ═════════════════════════════════════════════════════════════════════════
# 对外入口 4：文本优化 —— 去除多余符号
#   去掉 "*" 等不构成任何读法、只会干扰 TTS 合成 / 强制对齐 / 字典查词的
#   干扰符号。只删除这些"纯噪声"符号本身，不动符号两侧的文字，不影响
#   正常的中/英文标点（，。！？,.!?等）。
# ═════════════════════════════════════════════════════════════════════════

# 视为"纯噪声、无读法意义"的符号集合：Markdown 强调符号、下划线强调、
# 反引号代码标记、波浪号、竖线、方括号形式的注释标记等。刻意不包含任何
# 语言的正常标点（，。！？、；：""''（）—…,.!?;:"'()-）和本模块已支持
# 转换的符号（+-×÷=℃℉&%），避免"去除多余符号"把还没转换的符号也删掉。
_STRAY_SYMBOLS_RE = re.compile(r"[*_`~^\\|]")


def strip_stray_symbols(text: str) -> str:
    """去除 * 等不必要符号，仅删除符号本身，保留其余全部文字与标点。"""
    if not text:
        return text
    cleaned = _STRAY_SYMBOLS_RE.sub("", text)
    # 删除符号后同一行内可能残留因符号消失产生的多余空格（如 "**加粗**"
    # → "加粗"，这种情况不会产生多余空格；但 "a * b" → "a  b" 这种英文
    # 场景会，这里顺手清理，不影响中文场景）。
    lines = cleaned.split("\n")
    cleaned_lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in lines]
    return "\n".join(cleaned_lines)


# ═════════════════════════════════════════════════════════════════════════
# 对外入口（新增）：连字符转空格
#   常见于英文歌词/TTS 逐字对齐场景里用 "-" 连接音节或单词（如
#   "gon-na"、"good-bye"），朗读/合成时希望当作空格断开而不是保留连字符。
#   半角 "-" 与全角 "－" 都视为连字符，统一替换为一个空格；替换后同一行
#   内可能出现的多余空格按其它优化按钮的惯例一并清理。
# ═════════════════════════════════════════════════════════════════════════

_HYPHEN_RE = re.compile(r"[-－]")


def hyphen_to_space(text: str) -> str:
    """把文本中的连字符（半角 "-" / 全角 "－"）替换为空格。"""
    if not text:
        return text
    spaced = _HYPHEN_RE.sub(" ", text)
    lines = spaced.split("\n")
    cleaned_lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in lines]
    return "\n".join(cleaned_lines)


# ═════════════════════════════════════════════════════════════════════════
# 对外入口（新增）：按标点插入换行
#   三个按钮共用同一套"句末标点识别 + 保留标点本身、只在标点后插入换行"
#   的实现，唯一的区别是识别哪一组标点、以及"每几句"的分组大小：
#     · 按逗号插入换行：中/英文逗号（，,）每一个之后都换行
#     · 按句号插入换行：中/英文句号、感叹号、问号（。！？.!?）每一个之后
#       都换行（"句末"取广义——凡是能独立成句的终止标点都算）
#     · 按每几句插入换行：以断句标点（逗号，,、句号。！？.!? 均算一句）
#       为分句依据，每凑够 N 句换行一次，N 由前端弹窗里的数字输入框传入
#   已存在的换行符不受影响、不会被去重或改动；文本末尾不会补多余的换行。
# ═════════════════════════════════════════════════════════════════════════

# 逗号类（句中停顿，不构成完整句子）与句号类（句末终止，构成完整句子）
# 分开定义，"按每几句插入换行"用句号类计数，"按逗号/句号插入换行"两个
# 按钮分别只处理各自这一组、不碰另一组的标点。
_COMMA_PUNCT = "，,"
_PERIOD_PUNCT = "。！？.!?"

# 连续同类标点（如中文的"……"省略号场景、英文的"?!"）作为一个整体匹配，
# 只在这一串标点结束后插入一次换行，避免逐字符匹配产生多次换行/空行。
_COMMA_RUN_RE = re.compile(f"[{_COMMA_PUNCT}]+")
_PERIOD_RUN_RE = re.compile(f"[{_PERIOD_PUNCT}]+")


def _insert_newline_after(text: str, run_pattern: "re.Pattern") -> str:
    """在 run_pattern 匹配到的每一段连续标点后插入换行，标点本身保留
    不变。已经紧跟换行符的标点不会重复插入（避免产生空行）。"""
    if not text:
        return text

    def _replace(m: "re.Match") -> str:
        end = m.end()
        if end < len(text) and text[end] == "\n":
            return m.group(0)  # 后面本来就换行了，不重复插入
        return m.group(0) + "\n"

    return run_pattern.sub(_replace, text)


def newline_after_comma(text: str) -> str:
    """按逗号插入换行：中/英文逗号（，,）每一个之后换行，逗号本身保留。"""
    return _insert_newline_after(text, _COMMA_RUN_RE)


def newline_after_period(text: str) -> str:
    """按句号插入换行：中/英文句号/感叹号/问号（。！？.!?）每一个之后换行，
    标点本身保留。"""
    return _insert_newline_after(text, _PERIOD_RUN_RE)


# "每几句插入换行"里的"句"取广义——逗号类（句中停顿）和句号类（句末
# 终止）只要是"到达一个断句点"就各算一句，二者合并成一套断句符号集合，
# 与"按逗号插入换行"/"按句号插入换行"两个按钮分别只处理各自那一组
# 标点是两回事：这两个独立按钮需要精确区分逗号/句号，但"每几句换行"
# 只关心"多少个断句点之后该换行"，不区分断句点具体是逗号还是句号。
_SENTENCE_BREAK_PUNCT = _COMMA_PUNCT + _PERIOD_PUNCT


def newline_every_n_sentences(text: str, n: int = 2) -> str:
    """按每几句插入换行：只要遇到断句标点（逗号，,、句号。！？.!? 均算
    一句）就计数，每凑够 n 句换行一次；标点本身保留，只是在该插入的位置
    补插换行。n 非正数时按 2 处理（与前端弹窗默认值一致，避免传参异常时
    直接报错或死循环）。

    已存在的换行符视为天然的分段边界，会被保留在原位——本函数只在"该
    换行但原文还没换行"的位置补插换行，不会删除原文里任何已有的换行。
    """
    if not text:
        return text
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 2
    if n <= 0:
        n = 2

    out: List[str] = []
    count = 0
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        out.append(ch)
        if ch == "\n":
            count = 0  # 原文已有换行，重新从下一句开始计数
            i += 1
            continue
        if ch in _SENTENCE_BREAK_PUNCT:
            # 合并连续断句标点（如 "……"、"?!"、"，。"）为一句，避免
            # 重复计数（例如句号紧跟右引号+换行场景不会被拆成两句）。
            j = i + 1
            while j < length and text[j] in _SENTENCE_BREAK_PUNCT:
                out.append(text[j])
                j += 1
            count += 1
            i = j
            already_newline = i < length and text[i] == "\n"
            if count >= n and not already_newline:
                out.append("\n")
                count = 0
            continue
        i += 1
    return "".join(out)


# ═════════════════════════════════════════════════════════════════════════
# 统一调度入口：/api/text/optimize 直接调用这一个函数即可，按 action 分发
# 到上面各个转换函数之一。action 未知或为空时原样返回文本，不做任何改动
# （避免前端传参出错时静默产生意料之外的转换结果）。
# ═════════════════════════════════════════════════════════════════════════

_ACTIONS = {
    "smart": smart_convert,                 # 智能转换（需要 language）
    "number_only": number_only_convert,     # 仅转换（数字，完整数值读法，需要 language）
    "digit_to_words": digit_to_words_convert,  # 逐字转换（数字，按位读，需要 language）
    "symbol_only": symbol_only_convert,     # 仅转换符号（需要 language）
}

_ACTIONS_NO_LANG = {
    "add_spaces": add_spaces_around_english,      # 优化文本：英文首尾加空格（与语种无关）
    "strip_symbols": strip_stray_symbols,         # 优化文本：去除多余符号（与语种无关）
    "newline_after_comma": newline_after_comma,   # 优化文本：按逗号插入换行（与语种无关）
    "newline_after_period": newline_after_period, # 优化文本：按句号插入换行（与语种无关）
    "hyphen_to_space": hyphen_to_space,           # 优化文本：连字符转空格（与语种无关）
    "add_spaces_uppercase": add_spaces_around_uppercase,  # 优化文本：大写字母逐个加空格（与语种无关）
    "uppercase_to_lowercase": uppercase_to_lowercase,     # 优化文本：大写转小写（与语种无关）
    "lowercase_to_uppercase": lowercase_to_uppercase,     # 优化文本：小写转大写（与语种无关）
}


def process_text(text: str, action: str, language: str = "zh", n: int = 2) -> Dict[str, object]:
    """统一入口。

    Parameters
    ----------
    text: 待处理文本（仅处理这一段文本本身，不涉及任何文件/其它后端）。
    action: "smart" | "number_only" | "digit_to_words" | "symbol_only" |
      "add_spaces" | "strip_symbols" | "newline_after_comma" |
      "newline_after_period" | "newline_every_n" | "hyphen_to_space" |
      "add_spaces_uppercase" | "uppercase_to_lowercase" |
      "lowercase_to_uppercase"
    language: 语言代码（cmn/yue/eng/jpn/kor 或 zh/en/ja/ko），仅 smart /
      number_only / digit_to_words / symbol_only 需要，其余 action 与语种
      无关。
    n: 仅 "newline_every_n" 需要，表示"每几句插入一次换行"，默认 2。

    Returns
    -------
    Dict: {"success": True, "text": str} 或 {"success": False, "error": str}
    """
    text = text or ""
    if action == "newline_every_n":
        try:
            result = newline_every_n_sentences(text, n)
        except Exception as e:
            return {"success": False, "error": f"转换失败: {e}"}
        return {"success": True, "text": result}
    if action in _ACTIONS:
        try:
            result = _ACTIONS[action](text, language)
        except Exception as e:
            return {"success": False, "error": f"转换失败: {e}"}
        return {"success": True, "text": result}
    if action in _ACTIONS_NO_LANG:
        try:
            result = _ACTIONS_NO_LANG[action](text)
        except Exception as e:
            return {"success": False, "error": f"转换失败: {e}"}
        return {"success": True, "text": result}
    return {"success": False, "error": f"未知的转换类型: {action}"}
