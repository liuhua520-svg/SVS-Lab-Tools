# -*- coding: utf-8 -*-
"""
commandline.py — 命令行调用入口（不启动 HTTP 服务，单次执行即退出）

用途
────
让 app.py 除了作为常驻 HTTP 服务被 launcher.py 拉起之外，还能被直接以
命令行方式调用，一次性执行"标注提取 / 音高提取 / 工程文件生成 / 完整
处理"其中一个操作，不启动 Flask 服务、不占用 5850 端口，跑完立即退出，
方便写批处理脚本或接入其它命令行工具链。

用法（在 app.py 所在的 mfa_env 环境执行；打包后也可以直接
`启动器.exe cmd ...`，由 launcher.py 转发到这里，见 launcher.py 里的
_run_cmd_mode() 说明）：

    python app.py cmd mfa-only     -a in.wav -t "参考文本" [-o out.lab]
    python app.py cmd mfa-only     -a in.wav -T "文本文件.txt" [-o out.lab]
    python app.py cmd f0-only      -a in.wav [--method dio] [-o out.csv]
    python app.py cmd project-only -a in.wav --lab in.lab -f sv [-o out.svp]
    python app.py cmd full         -a in.wav -t "参考文本" -f sv [-o out.svp]
    python app.py cmd subtitle     -a in.wav -sf "字幕.srt" [-o out.lab]
    python app.py cmd dialogue-batch --manifest boxes.json -f sv [-o out.svp]
    python app.py cmd dialogue-batch --folder ./对话素材 -f sv [-o out.svp]
    python app.py cmd dict-import  mydict --file entries.csv
    python app.py cmd dict-list
    python app.py cmd dict-export  mydict [-o mydict.csv]
    python app.py cmd dict-edit    mydict set --word HELLO --phonemes "hh ah l ow"
    python app.py cmd dict-edit    mydict remove --word HELLO
    python app.py cmd asr-subtitle -a video.mp4 -f srt [-o out.srt]
    python app.py cmd settings-get
    python app.py cmd settings-set --set key=value [--set key2=value2]

    python app.py cmd <operation> --help   查看某个操作的完整参数列表

设计说明
────────
本模块刻意只做"解析参数 → 调用 app.py 里已经构建好的全局 pipeline
（AudioProcessingPipeline 实例）→ 把结果落盘/打印"这三件事，不重新实现
任何一处业务逻辑——mfa-only / project-only / full / dialogue-batch 几个
操作分别直接复用 pipeline.process_mfa_only() / process_project_only() /
process_full() / process_dialogue_batch()，与网页版 /api/pipeline/* 及
/api/dialogue/process 对应路由完全同源（参数含义、默认值都尽量保持
一致），保证命令行跑出来的结果和网页操作一致，不会出现"两套实现、
行为逐渐分叉"的问题。

f0-only 是唯一的例外：网页路由背后的 pipeline.process_f0_only() 只返回
帧数/采样率（给前端"测试"按钮展示概览用），并不保留 f0/t 数组、也不
落盘曲线文件——命令行下"音高提取"如果什么文件都不落盘，用户等于拿不到
任何产物。所以这里绕开 process_f0_only()，直接调用它内部同一份
tsubaki_processor.process_audio_f0()（提取算法/参数含义完全一致，只是
多导出一份 (time_sec, freq_hz) 的 CSV 曲线），详见 _cmd_f0_only() 内注释。

subtitle（字幕跟读）同样是"绕开异步任务包装、直接调用同一份底层函数"：
网页路由 /api/subtitle-import/align 背后是 run_subtitle_align_job()
起后台线程 + /api/pipeline/job/<id> 轮询这一套异步机制，命令行场景不需要
轮询，直接同步调用 subtitle_import.align_subtitle_audio()（字幕跟读固定
使用 Qwen3-ForcedAligner，与网页版行为一致，不提供 --aligner-backend
选项）+（--full 时）pipeline.process_project_only()，两步都执行完再返回，
本质和 run_subtitle_align_job() 是同一段逻辑，只是去掉了 Thread/Job 包装。

dialogue-batch（对话文本框批量处理）支持网页版里的两种输入模式，两种
框可以在同一份 --manifest 清单里混用：
    --manifest boxes.json ：JSON 数组，每个元素描述一个对话框，"audio"
        （音频跟读，现成的 wav）与 "tts"（TTS 跟读，只给文本+音色，
        音频当场用 tts_processor.synthesize_and_align() 合成+对齐，
        与网页版 run_dialogue_batch_job() 的 TTS 预处理是同一个函数）
        二选一：
        [{"text": "...", "audio": "a.wav", "lab": "a.lab"},
         {"tts": {"engine": "edge_tts", "voice": "...", "text": "..."}}, ...]
        字段含义见 dialogue-batch --help 和 _load_dialogue_manifest()
        内的说明。
    --folder DIR ：自动扫描目录，按"去扩展名后的文件名"配对同名的
        音频 + .lab/.mid/.midi/.txt，规则与网页版"导入文件夹（按文件名
        自动配对）"一致（见 _scan_dialogue_folder() 内注释）。只支持
        "音频跟读"——TTS 跟读没有现成音频文件可扫描，只能用 --manifest。
两者二选一，同时提供时 --manifest 优先。

asr-subtitle（字幕识别）对应网页版「字幕」页面（SubtitleEditor.vue）的
「上传媒体 → Qwen3-ASR 识别 → 导出字幕文本」这条主链路，同样是"绕开
异步任务包装、直接调用同一份底层函数"：网页路由 /api/subtitle/recognize
背后是 Thread + /api/subtitle/job/<id> 轮询，这里直接同步调用
subtitle_processor.transcribe_to_subtitles()，识别完立即用
subtitle_processor.export_subtitles() 把结果落盘成 SRT/LRC/TXT/LAB 文本
文件。与「字幕跟读」（subtitle 子命令）的方向正好相反：字幕跟读是"已有
文本字幕，只是没有精确时间轴，靠强制对齐补上时间轴"；asr-subtitle 是
"什么都没有，靠 Qwen3-ASR 从音频里识别出文本 + 时间轴"。【2026-08 起】
Qwen3-ASR 已迁入 .mfa_env 主进程内本地加载（不再是需要额外拉起的独立
微服务），此操作与 mfa-only 默认后端一样"本地直接跑，不需要额外微
服务"，命令行下开箱即用，不需要用户额外启动任何进程。

dict-edit（词典编辑）对应网页版「单词映射音素词典管理」页面里"新增/
编辑/删除单个词条"以及词典本身的新建/删除/改名这几个操作，与
dict-import/dict-export（整份文件级别的导入导出）互补——不需要为了改
一个单词就准备一份 CSV/JSON 文件。直接调用 dictionary_manager 对应的
单条目/词典级别函数（upsert_entry / delete_entry / create_dictionary /
delete_dictionary / rename_dictionary），与网页版
/api/dictionary/<source>/entry 等路由背后是同一批函数。

关于 -o/--output
─────────────────
pipeline.process_*() 内部产物固定落在 backend/work/ 工作目录下（与网页版
完全一致，方便复用已有的清理/调试机制）。命令行如果传了 -o/--output，
在原有落盘逻辑跑完之后，本模块再把结果文件"复制"一份到用户指定的路径/
文件名，不改变、也不侵入 pipeline.py 内部的落盘逻辑本身。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Callable, Optional

from pipeline import AudioProcessingPipeline
from tsubaki_processor import AudioProcessingConfig
import subtitle_import
import subtitle_processor
import dictionary_manager
import app_settings
import tts_processor

logger = logging.getLogger("commandline")

CMD_TRIGGER = "cmd"


def is_cmd_mode(argv=None) -> bool:
    """
    argv[1] == "cmd" → 命令行一次性调用模式；否则 → 正常启动 HTTP 服务
    （与改造前 `python app.py` 的默认行为完全一致，不受影响）。

    判断规则必须和 launcher.py 里 _is_cmd_invocation() 的判断逻辑完全
    一致（都是"argv[1] 精确等于 'cmd'"）——launcher.py 先判断一次决定要
    不要转发，转发过去之后 app.py 这边再判断一次决定要不要走 CmdUI，
    两处任何一处放宽/收紧匹配规则都要同步改另一处，否则会出现"已经被
    launcher 转发过来了，但 app.py 自己又判断不是 cmd 模式，转而去启动
    整个 HTTP 服务"这类不一致。
    """
    argv = sys.argv if argv is None else argv
    return len(argv) > 1 and argv[1] == CMD_TRIGGER


class _FileAdapter:
    """
    把磁盘上已存在的音频文件包装成 pipeline 期望的 audio_file 风格对象
    （.filename / .save() / .seek()），兼容 process_mfa_only() /
    process_full() 内部对 Flask FileStorage 接口的依赖。

    这是这套接口在项目里第三份几乎一样的小适配器（另外两份：app.py 的
    run_mfa_only_job() 里的 FileStorageWrapper、pipeline.py 内部的
    _LocalFileAdapter）——命令行模块刻意不去 import pipeline.py 里那个
    下划线开头的私有类，保持模块边界清晰，各自独立维护即可，反正就
    十来行。
    """

    def __init__(self, local_path: str):
        self.path = os.path.abspath(local_path)
        self.filename = os.path.basename(local_path)

    def save(self, dst: str) -> None:
        if os.path.abspath(dst) != self.path:
            shutil.copy(self.path, dst)

    def seek(self, *args, **kwargs) -> None:
        pass


def _read_text(text: Optional[str], text_file: Optional[str]) -> str:
    """-t/--text 优先；没有的话从 -T/--text-file 读（utf-8）；都没有则空字符串。"""
    if text:
        return text
    if text_file:
        return Path(text_file).read_text(encoding="utf-8")
    return ""


def _finalize_output(src_path: Optional[str], output: Optional[str], label: str) -> Optional[str]:
    """若传了 -o/--output，把落在 work_dir 里的产物再复制一份到用户指定路径。"""
    if not src_path:
        return None
    if not output:
        return src_path
    dst = Path(output)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_path, dst)
    print(f"✓ {label} 已复制到: {dst}")
    return str(dst)


ALIGNER_BACKENDS = ["mfa", "whisperx", "qwen3_asr", "qwen3_aligner", "nemo_aligner"]
F0_METHODS = ["dio", "harvest", "crepe", "rmvpe"]
F0_DEVICES = ["auto", "cpu", "cuda"]
CREPE_MODELS = ["full", "tiny"]
WHISPERX_MODELS = ["large-v3", "large-v3-turbo", "large-v2", "medium", "small", "base", "tiny"]
PHONEME_MODES = ["none", "merge", "hiragana", "katakana"]


def _scan_dialogue_folder(folder: str) -> list:
    """
    扫描目录，按"去扩展名后的文件名（大小写不敏感）"配对同名的音频文件
    与 .lab / .mid / .midi / .txt 文件，规则与网页版 DialogueBatch.vue 的
    "导入文件夹（按文件名自动配对）"一致：
      - 音频扩展名见 subtitle_processor.AUDIO_EXTS（wav/mp3/flac/m4a/aac/ogg/wma/opus）。
      - 同名 .lab 与 .mid/.midi 同时存在时优先使用 .lab。
      - 同名 .txt 存在时读作台词文本（可选；不存在则该框 text 为空，
        使用对齐后端自动转录/需要对齐后端支持纯 ASR）。
      - 没有配对到音频的文件忽略；没有音频只有孤立的 .lab/.txt 不会
        单独成框（与网页版一致——一个对话框必须有音频）。

    返回值是可以直接喂给 pipeline.process_dialogue_batch() 的 boxes 列表
    （按文件名排序，保证结果可复现），每项:
      {"index": int, "text": str, "audio_path": str,
       "lab_path": Optional[str], "midi_path": Optional[str]}
    """
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise ValueError(f"目录不存在: {folder}")

    by_stem: dict = {}
    for f in folder_path.iterdir():
        if not f.is_file():
            continue
        stem_key = f.stem.lower()
        ext = f.suffix.lower()
        entry = by_stem.setdefault(stem_key, {})
        if ext in subtitle_processor.AUDIO_EXTS:
            entry["audio"] = str(f)
        elif ext == ".lab":
            entry["lab"] = str(f)
        elif ext in (".mid", ".midi"):
            entry["midi"] = str(f)
        elif ext == ".txt":
            entry["txt"] = str(f)

    boxes = []
    for stem_key in sorted(by_stem.keys()):
        entry = by_stem[stem_key]
        audio_path = entry.get("audio")
        if not audio_path:
            continue  # 没有音频的孤立 lab/txt/midi 不单独成框
        text = ""
        if entry.get("txt"):
            text = Path(entry["txt"]).read_text(encoding="utf-8-sig").strip()
        boxes.append({
            "index": len(boxes),
            "text": text,
            "audio_path": audio_path,
            "lab_path": entry.get("lab"),
            "midi_path": entry.get("midi") if not entry.get("lab") else None,
        })
    return boxes


def _load_dialogue_manifest(manifest_path: str) -> list:
    """
    解析 --manifest 指定的 JSON 清单文件，格式:
      [{"text": "...", "audio": "a.wav", "lab": "a.lab", "midi": "a.mid",
        "align_pitch_shift": 0.0},
       {"tts": {"engine": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural",
                 "text": "...", "rate": "+0%", "pitch": "+0Hz", "volume": "+0%"}},
       ...]
    每一项 "audio" 与 "tts" 二选一（音频跟读 / TTS 跟读，两种框可以在同一份
    清单里混用）；同时提供时以 "audio" 为准，"tts" 被忽略（有现成音频就不用
    再合成）。"tts" 对象的字段直接对应
    tts_processor.synthesize_and_align() 的同名参数：
      - engine  : "edge_tts"（默认）/ "windows_sapi" / "qwen3_tts"
      - voice   : 音色 id；engine="qwen3_tts" 且 mode 为 voice_design/
                  voice_clone 时不需要
      - narrator_id : 可选，仅用于标记这一框是否对应网页版的"讲述人"
                  预设，不影响实际合成参数（合成仍完全由 engine/voice/
                  rate/pitch/volume/qwen3_tts_options 决定）。命令行本身
                  没有讲述人预设管理界面，这里只是让手写 manifest 时若想
                  在产物文件名里体现"这是讲述人配置"，可以随手填一个非空
                  字符串（如 "manual" 或预设名），留空则按 engine 生成
                  "tts_edgetts"/"tts_qwen3tts"标签。
      - text    : 该框台词文本；不传则回退使用外层的 "text" 字段
      - rate / pitch / volume : 语速/音调/音量，engine="qwen3_tts" 时不使用
      - qwen3_tts_options : 仅 engine="qwen3_tts" 时读取，见
                  tts_processor._qwen3_tts_synth_to_file() 顶部说明
                  （mode/instruct/ref_text/ref_audio_path/size/device 等，
                  voice_clone 模式直接在这里传 ref_audio_path 本地路径即可，
                  命令行不需要像网页版那样走文件上传）
    返回可直接喂给 process_dialogue_batch() 的 boxes 列表（"tts" 为 None
    的是音频跟读框，非 None 的是 TTS 跟读框，由 _cmd_dialogue_batch() 在
    调用 process_dialogue_batch() 之前先逐个合成+对齐、回填 audio_path/
    lab_path，见该方法内的说明）。
    """
    raw = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, list):
        raise ValueError("--manifest 指定的 JSON 文件顶层必须是数组")

    boxes = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"--manifest 第 {i + 1} 项必须是对象")
        has_audio = bool(item.get("audio"))
        tts_info = item.get("tts") if not has_audio else None
        if not has_audio and not tts_info:
            raise ValueError(f'--manifest 第 {i + 1} 项需要提供 "audio" 或 "tts" 之一')
        if tts_info and not (tts_info.get("text") or item.get("text")):
            raise ValueError(f"--manifest 第 {i + 1} 项（TTS 跟读）需要 tts.text 或外层 text 提供台词文本")
        boxes.append({
            "index": len(boxes),
            "text": item.get("text") or (tts_info or {}).get("text") or "",
            "audio_path": item.get("audio") if has_audio else None,
            "lab_path": item.get("lab") if has_audio else None,
            "midi_path": (item.get("midi") if not item.get("lab") else None) if has_audio else None,
            "align_pitch_shift_semitones": item.get("align_pitch_shift", 0.0),
            "tts": tts_info,
        })
    return boxes


def _extract_dict_entries_from_json(payload, source: str):
    """
    与 app.py 里 _extract_entries_from_json_payload() 逻辑一致（故意复制
    一份而不是相互 import，保持 commandline.py 不反向依赖 app.py 的私有
    函数——参见 _FileAdapter 处的同类说明）：尽量宽松地从导入的 JSON 中
    解析出 {WORD: phonemes} 词条字典，支持三种形状：
      1) 扁平格式：{"WORD": "phones", ...}
      2) 单词典导出格式：{"notation": "...", "entries": {...}}
      3) 带词典名包裹：{"<词典名>": {"notation":...,"entries":{...}}} 或
         {"<词典名>": {"WORD": "phones", ...}}
    """
    if not isinstance(payload, dict) or not payload:
        return None

    if isinstance(payload.get("entries"), dict):
        return payload["entries"]

    candidates = [payload[source]] if source in payload else list(payload.values())
    for inner in candidates:
        if isinstance(inner, dict):
            if isinstance(inner.get("entries"), dict):
                return inner["entries"]
            if inner and all(not isinstance(v, dict) for v in inner.values()):
                return inner

    if all(not isinstance(v, dict) for v in payload.values()):
        return payload

    return None


def _coerce_settings_value(raw: str):
    """--set key=value 的 value 部分按常见类型自动推断：true/false → bool，
    看起来像整数/小数的 → int/float，其余原样保留字符串。"""
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


class CmdUI:
    """命令行调用入口：解析参数 → 调用 pipeline → 打印/落盘结果。"""

    def __init__(
        self,
        pipeline: AudioProcessingPipeline,
        select_vsqx_singer: Optional[Callable[[str, str], tuple]] = None,
    ):
        self.pipeline = pipeline
        # app.py 里已经有一份按语种选默认 VSQX 声库的 _select_vsqx_singer()，
        # 由 app.py 在构造 CmdUI 时把这个函数对象直接传进来复用（而不是在
        # 这里 `from app import _select_vsqx_singer`）——直接 import app
        # 模块名会导致 Python 把 app.py 当成一个新模块重新执行一遍模块顶层
        # 代码（`python app.py` 直接运行时脚本本身是 __main__，而不是叫
        # "app" 的模块），等于把 Flask app / pipeline / MFAProcessor 全部
        # 重新初始化一遍，代价很大也没必要，传函数引用最干净。
        self._select_vsqx_singer = select_vsqx_singer or (
            lambda language, mode: ("MIKU_V4X_Original_EVEC", "BCNFCY43LB2LZCD4", 0)
        )

    # ── 参数解析 ──────────────────────────────────────────────────────
    def _build_parser(self) -> argparse.ArgumentParser:
        # --json 既想在子命令前面写（`cmd --json mfa-only ...`），也想在
        # 子命令后面/参数中间写（`cmd mfa-only -a in.wav --json` 或文档里
        # 示例那种写在末尾 `-o out.lab --json`）——argparse 的 subparsers
        # 机制下，主解析器不认识"子命令之后"出现的、只在主解析器上定义过
        # 的选项（本质是两个独立的解析器，各自只认自己定义的参数）。用一个
        # 共享的 parent parser 把 --json 同时注册到每个子命令解析器上，
        # 两种写法就都能识别到同一个 args.json，不用强制用户记"必须写在
        # 子命令前面"这种反直觉规则。
        json_flag_parent = argparse.ArgumentParser(add_help=False)
        json_flag_parent.add_argument(
            "--json", action="store_true",
            help="额外打印一份机器可读的 JSON 结果到 stdout（便于脚本解析）",
        )

        parser = argparse.ArgumentParser(
            prog="app.py cmd",
            description=(
                "命令行一次性调用（不启动 HTTP 服务），例如:\n"
                '  app.py cmd mfa-only -a in.wav -t "参考文本" -o out.lab\n'
                "  app.py cmd f0-only -a in.wav -o out.csv\n"
                "  app.py cmd project-only -a in.wav --lab in.lab -f sv -o out.svp\n"
                '  app.py cmd full -a in.wav -t "参考文本" -f sv -o out.svp\n'
                "\n"
                "--json 加在子命令前后均可，例如两种写法等价:\n"
                '  app.py cmd --json mfa-only -a in.wav -t "文本"\n'
                '  app.py cmd mfa-only -a in.wav -t "文本" --json\n'
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
            parents=[json_flag_parent],
        )
        sub = parser.add_subparsers(dest="operation", required=True, metavar="operation")

        # ── mfa-only（标注提取）──────────────────────────────────────
        p = sub.add_parser("mfa-only", aliases=["lab"], help="仅执行标注提取，产出 LAB 文件",
                            parents=[json_flag_parent])
        p.add_argument("-a", "--audio", required=True, help="输入音频文件路径 (wav)")
        p.add_argument("-t", "--text", help="参考文本（与 --text-file 二选一）")
        p.add_argument("-T", "--text-file", help="从文件读取参考文本 (utf-8)，与 --text 二选一")
        p.add_argument("-l", "--language", default="cmn", help="语种代码，默认 cmn")
        p.add_argument("--aligner-backend", default="mfa", choices=ALIGNER_BACKENDS, help="对齐后端，默认 mfa")
        p.add_argument("--aligner-device", default=None, help="对齐后端运行设备，默认跟随 --f0-device")
        p.add_argument("--f0-device", default="auto", choices=F0_DEVICES)
        p.add_argument("--whisperx-model", default="large-v3", choices=WHISPERX_MODELS)
        p.add_argument("--whisperx-batch-size", type=int, default=16)
        p.add_argument("--qwen3-batch-size", type=int, default=8)
        p.add_argument("--nemo-model", default=None, help="NeMo Forced Aligner 模型覆盖（可选）")
        p.add_argument("--english-word-align", action="store_true", help="启用英语单词级对齐")
        p.add_argument("--ja-disable-katakana", action="store_true",
                        help="关闭英语转片假名（仅日语 jpn 有意义）：文本中的英语单词不再转换为片假名读音")
        p.add_argument("--align-pitch-shift", type=float, default=0.0, metavar="SEMITONES", help="对齐辅助移调（半音）")
        p.add_argument("-o", "--output", help="LAB 输出路径（不传则只保留在 backend/work/ 下）")
        p.set_defaults(func=self._cmd_mfa_only)

        # ── f0-only（音高提取）──────────────────────────────────────
        p = sub.add_parser("f0-only", aliases=["pitch"], help="仅执行音高(F0)提取，产出 CSV 曲线",
                            parents=[json_flag_parent])
        p.add_argument("-a", "--audio", required=True, help="输入音频文件路径 (wav)")
        p.add_argument("--method", default="dio", choices=F0_METHODS)
        p.add_argument("--f0-floor", type=float, default=71.0)
        p.add_argument("--f0-ceil", type=float, default=800.0)
        p.add_argument("--no-smooth", action="store_true", help="关闭平滑（默认开启）")
        p.add_argument("--smooth-window", type=int, default=5)
        p.add_argument("--double-precision", action="store_true")
        p.add_argument("--f0-device", default="auto", choices=F0_DEVICES)
        p.add_argument("--crepe-model", default="full", choices=CREPE_MODELS)
        p.add_argument("-o", "--output", help="F0 曲线 CSV 输出路径（默认: 音频同目录 <stem>.f0.csv）")
        p.set_defaults(func=self._cmd_f0_only)

        # ── project-only（工程文件输出）──────────────────────────────
        p = sub.add_parser("project-only", aliases=["project"], help="仅执行工程文件生成（已有 WAV + LAB/MIDI）",
                            parents=[json_flag_parent])
        p.add_argument("-a", "--audio", required=True, help="输入音频文件路径 (wav)")
        p.add_argument("--lab", help="LAB 标注文件路径（与 --midi 至少提供一个）")
        p.add_argument("--midi", help="MIDI 文件路径（与 --lab 至少提供一个）")
        p.add_argument("-f", "--format", default="sv", help="输出格式: sv/ustx/utau/midi/vsqx（含别名 svp/openutau/vocaloid 等），默认 sv")
        p.add_argument("--title", default="Project")
        p.add_argument("-l", "--language", default="", help="语种（可选，供工程构建器判断，不传则按内容自动判断）")
        p.add_argument("--bpm", type=float, default=120.0)
        p.add_argument("--base-pitch", type=int, default=60)
        p.add_argument("--f0-method", default="dio", choices=F0_METHODS)
        p.add_argument("--f0-floor", type=float, default=71.0)
        p.add_argument("--f0-ceil", type=float, default=800.0)
        p.add_argument("--no-smooth", action="store_true")
        p.add_argument("--smooth-window", type=int, default=5)
        p.add_argument("--double-precision", action="store_true")
        p.add_argument("--f0-device", default="auto", choices=F0_DEVICES)
        p.add_argument("--crepe-model", default="full", choices=CREPE_MODELS)
        p.add_argument("--vsqx-pitch-smooth-window", type=int, default=5)
        p.add_argument("--auto-note-pitch", action="store_true", help="用 F0 中位音高细化音符音高")
        p.add_argument("--export-pitch-line", action="store_true", help="将 F0 曲线写入工程文件（默认关闭，与网页版一致）")
        p.add_argument("--phoneme-mode", default="none", choices=PHONEME_MODES)
        p.add_argument("--lyrics-text", default="")
        p.add_argument("--original-text", default="", help="原始歌词文本，用于预提取英语单词（可选）")
        p.add_argument("--vsqx-singer", default=None, help="不传则按 language 自动选择（仅生成工程模式默认日语声库）")
        p.add_argument("--vsqx-singer-id", default=None)
        p.add_argument("--vsqx-singer-bs", type=int, default=None)
        p.add_argument("--word-phoneme-map", action="store_true")
        p.add_argument("--dict-source", default="default")
        p.add_argument("--ja-devoiced-phoneme", action="store_true")
        p.add_argument("--fill-short-rests", action="store_true", help="自动填充短休止符（开关）")
        p.add_argument("--fill-short-rests-max-length", default="16", choices=["8", "16", "32", "64", "128"],
                        help="判定“短”的音符时值阈值，仅 --fill-short-rests 开启时生效")
        p.add_argument("-o", "--output", help="工程文件输出路径（不传则只保留在 backend/work/ 下）")
        p.set_defaults(func=self._cmd_project_only)

        # ── full（完整处理：标注 + 音高 + 工程文件）────────────────────
        p = sub.add_parser("full", help="完整处理：标注提取 + 音高提取 + 工程文件生成",
                            parents=[json_flag_parent])
        p.add_argument("-a", "--audio", required=True)
        p.add_argument("-t", "--text", help="参考文本（与 --text-file 二选一）")
        p.add_argument("-T", "--text-file", help="从文件读取参考文本 (utf-8)")
        p.add_argument("-l", "--language", default="cmn")
        p.add_argument("-f", "--format", default="sv")
        p.add_argument("--title", default="Project")
        p.add_argument("--bpm", type=float, default=120.0)
        p.add_argument("--base-pitch", type=int, default=60)
        p.add_argument("--f0-method", default="dio", choices=F0_METHODS)
        p.add_argument("--f0-floor", type=float, default=71.0)
        p.add_argument("--f0-ceil", type=float, default=800.0)
        p.add_argument("--no-smooth", action="store_true")
        p.add_argument("--smooth-window", type=int, default=5)
        p.add_argument("--double-precision", action="store_true")
        p.add_argument("--f0-device", default="auto", choices=F0_DEVICES)
        p.add_argument("--crepe-model", default="full", choices=CREPE_MODELS)
        p.add_argument("--vsqx-pitch-smooth-window", type=int, default=5)
        p.add_argument("--auto-note-pitch", action="store_true")
        p.add_argument("--no-pitch-line", action="store_true", help="不将 F0 曲线写入工程文件（默认写入）")
        p.add_argument("--aligner-backend", default="mfa", choices=ALIGNER_BACKENDS)
        p.add_argument("--aligner-device", default="auto")
        p.add_argument("--whisperx-model", default="large-v3", choices=WHISPERX_MODELS)
        p.add_argument("--whisperx-batch-size", type=int, default=16)
        p.add_argument("--qwen3-batch-size", type=int, default=8)
        p.add_argument("--nemo-model", default=None)
        p.add_argument("--english-word-align", action="store_true")
        p.add_argument("--ja-disable-katakana", action="store_true",
                        help="关闭英语转片假名（仅日语 jpn 有意义）：文本中的英语单词不再转换为片假名读音")
        p.add_argument("--word-phoneme-map", action="store_true")
        p.add_argument("--align-pitch-shift", type=float, default=0.0, metavar="SEMITONES")
        p.add_argument("--dict-source", default="default")
        p.add_argument("--vsqx-singer", default=None, help="不传则按 --language 自动选择（仅 format=vsqx 时生效）")
        p.add_argument("--vsqx-singer-id", default=None)
        p.add_argument("--vsqx-singer-bs", type=int, default=None)
        p.add_argument("--fill-short-rests", action="store_true", help="自动填充短休止符（开关）")
        p.add_argument("--fill-short-rests-max-length", default="16", choices=["8", "16", "32", "64", "128"],
                        help="判定“短”的音符时值阈值，仅 --fill-short-rests 开启时生效")
        p.add_argument("-o", "--output", help="工程文件输出路径（不传则只保留在 backend/work/ 下）")
        p.set_defaults(func=self._cmd_full)

        # ── subtitle（字幕跟读）────────────────────────────────────
        # 与网页版「单文件处理 → 字幕跟读」对应：整段音频 + 一份 SRT/LRC
        # 字幕，按字幕时间轴逐句固定用 Qwen3-ForcedAligner 强制对齐，产出
        # 覆盖整段音频的 LAB；--full 时继续生成工程文件（等价网页版的
        # "完整处理"，不传则等价"仅标注(快速)"）。
        p = sub.add_parser("subtitle", aliases=["sub"], help="字幕跟读：按 SRT/LRC 时间轴逐句强制对齐",
                            parents=[json_flag_parent])
        p.add_argument("-a", "--audio", required=True, help="输入音频文件路径")
        p.add_argument("-sf", "--subtitle-file", required=True, help="SRT / LRC / LAB 字幕文件路径（格式自动判断）")
        p.add_argument("-l", "--language", default="cmn", help="语种代码，默认 cmn")
        p.add_argument("--aligner-device", default="auto", choices=F0_DEVICES,
                        help="Qwen3-ForcedAligner 运行设备，默认 auto（字幕跟读固定使用该后端，无 --aligner-backend 选项）")
        p.add_argument("--english-word-align", action="store_true", help="启用英语单词级对齐")
        p.add_argument("--ja-disable-katakana", action="store_true",
                        help="关闭英语转片假名（仅日语 jpn 有意义）：文本中的英语单词不再转换为片假名读音")
        p.add_argument("--align-pitch-shift", type=float, default=0.0, metavar="SEMITONES", help="对齐辅助移调（半音）")
        p.add_argument("--skip-split-every-n", type=int, default=None,
                        help="每 N 条字幕合并成一个对齐块（不传则读取设置页保存的全局值，"
                             "与网页版“字幕每多少个时间轴跳过分割音频”是同一个设置项）")
        p.add_argument("--full", action="store_true",
                        help="完整处理：额外生成工程文件（对应网页版“完整处理”）；"
                             "不传则只产出 LAB（对应网页版“仅标注(快速)”）")
        # 以下参数仅在 --full 时使用，语义与 project-only 子命令一致。
        p.add_argument("-f", "--format", default="sv", help="仅 --full 时使用")
        p.add_argument("--title", default="Subtitle Project")
        p.add_argument("--bpm", type=float, default=120.0)
        p.add_argument("--base-pitch", type=int, default=60)
        p.add_argument("--f0-method", default="dio", choices=F0_METHODS)
        p.add_argument("--f0-floor", type=float, default=71.0)
        p.add_argument("--f0-ceil", type=float, default=800.0)
        p.add_argument("--no-smooth", action="store_true")
        p.add_argument("--smooth-window", type=int, default=5)
        p.add_argument("--double-precision", action="store_true")
        p.add_argument("--f0-device", default="auto", choices=F0_DEVICES)
        p.add_argument("--crepe-model", default="full", choices=CREPE_MODELS)
        p.add_argument("--vsqx-pitch-smooth-window", type=int, default=5)
        p.add_argument("--auto-note-pitch", action="store_true")
        p.add_argument("--no-pitch-line", action="store_true", help="不将 F0 曲线写入工程文件（默认写入）")
        p.add_argument("--word-phoneme-map", action="store_true")
        p.add_argument("--dict-source", default="default")
        p.add_argument("--vsqx-singer", default=None)
        p.add_argument("--vsqx-singer-id", default=None)
        p.add_argument("--vsqx-singer-bs", type=int, default=None)
        p.add_argument("-o", "--output", help="LAB（默认）或工程文件（--full 时）输出路径")
        p.set_defaults(func=self._cmd_subtitle)

        # ── asr-subtitle（字幕识别）────────────────────────────────
        # 与 subtitle 子命令方向相反：subtitle 是"已有文本字幕，靠强制
        # 对齐补时间轴"；这里是"什么都没有，靠 Qwen3-ASR 从音频/视频里
        # 识别出文本 + 时间轴"，对应网页版「字幕」页面的识别 + 导出。
        # 【2026-08 起】Qwen3-ASR 已迁入 .mfa_env 主进程内本地加载，本
        # 命令开箱即用，不需要额外启动独立微服务。
        p = sub.add_parser("asr-subtitle", aliases=["asr", "subtitle-recognize"],
                            help="用 Qwen3-ASR 识别音频/视频里的语音，导出字幕文本文件",
                            parents=[json_flag_parent])
        p.add_argument("-a", "--audio", required=True, help="输入音频或视频文件路径")
        p.add_argument("-l", "--language", default="auto",
                        help='语言代码，默认 "auto"（自动检测），常见取值: zh/en/ja/ko/fr/de/es/ru 等 '
                             "(完整列表见 subtitle_processor.resolve_qwen3_language())")
        p.add_argument("--device", default="auto", choices=F0_DEVICES, help="Qwen3-ASR 本地推理使用的运行设备")
        p.add_argument("--batch-size", type=int, default=8, help="Qwen3-ASR 推理批大小")
        p.add_argument("--max-chars", type=int, default=None,
                        help="单条字幕最大字符数，超过则按标点二次拆分（不传则用 subtitle_processor 内置默认值）")
        p.add_argument("--split-at-sentence-end", action="store_true",
                        help="无条件遇到句末标点（。！？；等）就切成下一条字幕，与长度无关（默认关闭，"
                             "关闭时只有整段超过 --max-chars 才在句末标点处二次拆分）")
        p.add_argument("--allow-comma-split", action="store_true",
                        help="连逗号/顿号也当作切分点（仅 --split-at-sentence-end 开启时生效）")
        p.add_argument("--remove-punctuation", action="store_true", help="识别结果中移除标点符号")
        p.add_argument("--close-vad-gaps", action="store_true",
                        help="相邻字幕间静音间隔大于 --vad-gap-threshold 时对半分配到中点，让时间轴更紧凑")
        p.add_argument("--vad-gap-threshold", type=float, default=0.6, metavar="SECONDS",
                        help="触发 --close-vad-gaps 处理的间隔下限（秒）")
        p.add_argument("-f", "--format", default="srt", choices=["srt", "lrc", "txt", "lab"],
                        help="导出字幕格式，默认 srt")
        p.add_argument("-o", "--output", help="字幕输出路径（默认: 音频同目录 <stem>.<format>）")
        p.set_defaults(func=self._cmd_asr_subtitle)

        # ── dialogue-batch（对话文本框批量处理）───────────────────────
        # 仅支持"音频跟读"输入模式（不支持网页版的"TTS跟读"，见模块顶部
        # 说明）。--manifest 与 --folder 二选一，同时提供时 --manifest 优先。
        p = sub.add_parser("dialogue-batch", aliases=["dialogue"],
                            help="对话文本框批量处理（音频跟读 或 TTS 跟读，按每个对话框在 --manifest 里二选一）",
                            parents=[json_flag_parent])
        p.add_argument("--manifest", default=None,
                        help='JSON 清单文件路径，数组，每项 {"text":str可选,"audio":str,'
                             '"lab":str可选,"midi":str可选,"align_pitch_shift":float可选} '
                             '（音频跟读），或 {"tts":{"engine":str,"voice":str,"text":str,'
                             '"rate":str可选,"pitch":str可选,"volume":str可选,'
                             '"qwen3_tts_options":object可选}} （TTS 跟读，音频当场合成，'
                             '与 "audio" 二选一，同时提供时以 "audio" 为准）；'
                             '两种框可以在同一份清单里混用')
        p.add_argument("--folder", default=None,
                        help="目录路径，自动按同名文件配对音频 + .lab/.mid/.midi/.txt（仅适用于音频跟读，"
                             "TTS 跟读没有现成音频文件可扫描，只能用 --manifest；配对规则与网页版"
                             "“导入文件夹（按文件名自动配对）”一致，见 --help 顶部模块说明）")
        p.add_argument("-l", "--language", default="cmn", help="语种代码，默认 cmn")
        p.add_argument("-f", "--format", default="sv", choices=["sv", "vsqx", "ustx"],
                        help="对话文本框批量处理不支持 MIDI 标准文件（单音轨概念），默认 sv")
        p.add_argument("--title", default="Dialogue Project")
        p.add_argument("--processing-mode", default="full", choices=["full", "project-only"],
                        help="full: 没有 LAB/MIDI 的对话框走对齐；project-only: 仅使用已提供的 LAB/MIDI，其余跳过")
        p.add_argument("--phoneme-mode", default="none", choices=PHONEME_MODES)
        p.add_argument("--ja-devoiced-phoneme", action="store_true")
        p.add_argument("--bpm", type=float, default=120.0)
        p.add_argument("--base-pitch", type=int, default=60)
        p.add_argument("--f0-method", default="dio", choices=F0_METHODS)
        p.add_argument("--f0-floor", type=float, default=71.0)
        p.add_argument("--f0-ceil", type=float, default=800.0)
        p.add_argument("--no-smooth", action="store_true")
        p.add_argument("--smooth-window", type=int, default=5)
        p.add_argument("--double-precision", action="store_true")
        p.add_argument("--f0-device", default="auto", choices=F0_DEVICES)
        p.add_argument("--crepe-model", default="full", choices=CREPE_MODELS)
        p.add_argument("--vsqx-pitch-smooth-window", type=int, default=5)
        p.add_argument("--auto-note-pitch", action="store_true")
        p.add_argument("--no-pitch-line", action="store_true", help="不将 F0 曲线写入工程文件（默认写入）")
        p.add_argument("--aligner-backend", default="mfa", choices=ALIGNER_BACKENDS,
                        help="没有 LAB/MIDI 的对话框走对齐时使用的后端，默认 mfa")
        p.add_argument("--aligner-device", default=None, help="默认跟随 --f0-device")
        p.add_argument("--whisperx-model", default="large-v3", choices=WHISPERX_MODELS)
        p.add_argument("--whisperx-batch-size", type=int, default=16)
        p.add_argument("--qwen3-batch-size", type=int, default=8)
        p.add_argument("--nemo-model", default=None)
        p.add_argument("--english-word-align", action="store_true")
        p.add_argument("--ja-disable-katakana", action="store_true",
                        help="关闭英语转片假名（仅日语 jpn 有意义）：文本中的英语单词不再转换为片假名读音；"
                             "整批全局默认值，可被每个对话框的 manifest override 覆盖")
        p.add_argument("--word-phoneme-map", action="store_true")
        p.add_argument("--dict-source", default="default")
        p.add_argument("--vsqx-singer", default=None)
        p.add_argument("--vsqx-singer-id", default=None)
        p.add_argument("--vsqx-singer-bs", type=int, default=None)
        p.add_argument("--fill-short-rests", action="store_true", help="自动填充短休止符（开关，整批默认值，可被每个对话框的 manifest 覆盖）")
        p.add_argument("--fill-short-rests-max-length", default="16", choices=["8", "16", "32", "64", "128"],
                        help="判定“短”的音符时值阈值，仅 --fill-short-rests 开启时生效")
        p.add_argument("-o", "--output", help="工程文件输出路径（不传则只保留在 backend/work/ 下）")
        p.set_defaults(func=self._cmd_dialogue_batch)

        # ── dict-import（词典加载）─────────────────────────────────
        p = sub.add_parser("dict-import", aliases=["dict-load"], help="从 CSV/JSON 文件导入词条到指定词典（不存在则自动创建）",
                            parents=[json_flag_parent])
        p.add_argument("name", help="目标词典名（单词映射音素词典管理里的词典名）")
        p.add_argument("--file", required=True, help="要导入的 .csv 或 .json 文件路径")
        p.add_argument("--notation", default="synthesizerv", choices=list(dictionary_manager.VALID_NOTATIONS),
                        help="仅在自动创建新词典时生效（词典已存在则忽略），默认 synthesizerv")
        p.add_argument("--no-overwrite", action="store_true",
                        help="已存在的同名单词（大小写不敏感）跳过而不是覆盖，默认覆盖")
        p.set_defaults(func=self._cmd_dict_import)

        # ── dict-list（词典列表）───────────────────────────────────
        p = sub.add_parser("dict-list", help="列出所有独立词典及词条数量", parents=[json_flag_parent])
        p.set_defaults(func=self._cmd_dict_list)

        # ── dict-export（词典导出）─────────────────────────────────
        p = sub.add_parser("dict-export", help="导出指定词典为 CSV 或 JSON", parents=[json_flag_parent])
        p.add_argument("name", help="要导出的词典名")
        p.add_argument("--format", default="csv", choices=["csv", "json"])
        p.add_argument("-o", "--output", help="输出文件路径（不传则打印到 stdout）")
        p.set_defaults(func=self._cmd_dict_export)

        # ── dict-edit（词典编辑：单条目增删 / 词典本身增删改名）───────
        # 与 dict-import/dict-export（整份文件级别）互补：不需要为了改
        # 一个单词就准备一份 CSV/JSON 文件。
        p = sub.add_parser("dict-edit", help="编辑单个词典：增/删单个词条，或新建/删除/改名整个词典",
                            parents=[json_flag_parent])
        p.add_argument("name", help="词典名")
        p.add_argument("action", choices=["set", "remove", "create", "delete", "rename"],
                        help="set=新增/更新一个词条；remove=删除一个词条；"
                             "create=新建空词典；delete=删除整个词典；rename=词典改名")
        p.add_argument("--word", default=None, help="单词（action=set/remove 时必填）")
        p.add_argument("--phonemes", default=None,
                        help='音素序列（action=set 时必填，如 "hh ah l ow"；记号规则不强制校验，'
                             "原样存储、原样使用，与网页版一致）")
        p.add_argument("--notation", default=None, choices=list(dictionary_manager.VALID_NOTATIONS),
                        help="记号体系（action=create 时使用，不传默认 synthesizerv；对已存在的词典无效——"
                             "记号体系在词典创建时确定，不能通过本命令更改已有词典的记号）")
        p.add_argument("--new-name", default=None, help="新词典名（action=rename 时必填）")
        p.set_defaults(func=self._cmd_dict_edit)

        # ── settings-get / settings-set（全局设置）─────────────────
        p = sub.add_parser("settings-get", help="打印当前全局设置", parents=[json_flag_parent])
        p.set_defaults(func=self._cmd_settings_get)

        p = sub.add_parser("settings-set", help="更新全局设置（等价网页版“设置”页保存）",
                            parents=[json_flag_parent])
        p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="可重复传入，例如 --set hf_hub_offline=true --set download_mirror=true；"
                             "VALUE 会按 true/false/整数/小数/字符串 自动推断类型")
        p.set_defaults(func=self._cmd_settings_set)

        return parser

    # ── 入口 ──────────────────────────────────────────────────────────
    def run(self, argv) -> int:
        """
        argv 是完整的 sys.argv（argv[0]=脚本名, argv[1]="cmd", argv[2]=操作名...）。
        返回进程退出码：0 成功，1 失败/异常，2 参数错误（argparse 自身约定，
        比如缺少必填参数、--format 传了不认识的值这类）。

        供真正的命令行调用使用（launcher.py 转发过来的 `exe cmd ...`，或
        `python app.py cmd ...`）。HTTP 路由（/api/cmd/exec）不走这个方法，
        走下面的 run_args()，避免为了拿一个 dict 结果去折腾进程退出码。
        """
        parser = self._build_parser()
        args = parser.parse_args(argv[2:])  # 跳过脚本名 + "cmd"
        try:
            result = args.func(args)
        except Exception as e:
            logger.exception("命令行操作异常")
            print(f"✗ 执行异常: {e}", file=sys.stderr)
            return 1

        ok = bool(result.get("success"))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, default=str))
        if not ok:
            print(f"✗ 失败: {result.get('error', '未知错误')}", file=sys.stderr)
        return 0 if ok else 1

    def run_args(self, arg_list) -> dict:
        """
        与 run() 等价的“库调用”版本：接收不含脚本名/"cmd"的参数列表
        （即 sys.argv[2:] 那一段，例如 ["mfa-only", "-a", "in.wav", "-t", "文本"]），
        直接返回各 _cmd_*() 产出的结果 dict，不打印、不 sys.exit、不吞异常。

        用途：/api/cmd/exec 路由（app.py 进程内，服务端直接执行一次命令行
        等效操作，浏览器发 JSON 参数过来，不需要真的起子进程/拼命令行
        字符串）。argparse 的用法错误（缺参数、枚举值不对）仍按 argparse
        约定抛 SystemExit(2)，调用方（app.py 路由）负责捕获并转成 400。
        """
        parser = self._build_parser()
        args = parser.parse_args(arg_list)
        return args.func(args)

    # ── 各操作实现（全部同步阻塞执行，跑完即返回结果 dict）────────────

    def _cmd_mfa_only(self, args) -> dict:
        text = _read_text(args.text, args.text_file)
        text_optional = args.aligner_backend in ("whisperx", "qwen3_asr")
        if not text and not text_optional:
            raise ValueError("该对齐后端需要参考文本，请提供 -t/--text 或 -T/--text-file")
        if not os.path.exists(args.audio):
            raise ValueError(f"音频文件不存在: {args.audio}")

        audio_file = _FileAdapter(args.audio)
        result = self.pipeline.process_mfa_only(
            audio_file, text, args.language,
            aligner_backend=args.aligner_backend,
            f0_device=args.f0_device,
            aligner_device=args.aligner_device,
            whisperx_model=args.whisperx_model,
            whisperx_batch_size=args.whisperx_batch_size,
            qwen3_batch_size=args.qwen3_batch_size,
            nemo_model=args.nemo_model,
            english_word_align=args.english_word_align,
            ja_disable_katakana=args.ja_disable_katakana,
            align_pitch_shift_semitones=args.align_pitch_shift,
        )
        if result.get("success"):
            print(f"✓ 标注提取完成: {result.get('lab_path')}")
            result["final_output_path"] = _finalize_output(result.get("lab_path"), args.output, "LAB 文件")
        return result

    def _cmd_f0_only(self, args) -> dict:
        # 有意不调用 pipeline.process_f0_only()：该方法只返回帧数/采样率，
        # 不保留 f0/t 数组，没法落盘曲线文件——原因见本文件顶部说明。这里
        # 直接复用它内部同一份 tsubaki_processor.process_audio_f0()，提取
        # 算法/参数含义与网页版完全一致，只是多导出一份 CSV。
        if not os.path.exists(args.audio):
            raise ValueError(f"音频文件不存在: {args.audio}")

        config = AudioProcessingConfig(
            f0_method=args.method,
            f0_floor=args.f0_floor,
            f0_ceil=args.f0_ceil,
            f0_smooth=not args.no_smooth,
            f0_smooth_window=args.smooth_window,
            use_double_precision=args.double_precision,
            f0_device=args.f0_device,
            crepe_model=args.crepe_model,
        )
        audio_data = self.pipeline.tsubaki_processor.process_audio_f0(args.audio, config)
        if not audio_data or not audio_data.get("success"):
            return {"success": False, "error": (audio_data or {}).get("error", "F0 提取失败")}

        f0, t = audio_data["f0"], audio_data["t"]
        out_path = Path(args.output) if args.output else (
            Path(args.audio).resolve().with_name(Path(args.audio).stem + ".f0.csv")
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("time_sec,freq_hz\n")
            for ti, fi in zip(t, f0):
                f.write(f"{ti:.6f},{fi:.4f}\n")

        print(f"✓ 音高提取完成: {len(f0)} 帧，已写入 {out_path}")
        return {
            "success": True,
            "method": audio_data.get("method"),
            "frames": len(f0),
            "sample_rate": audio_data.get("sr", 0),
            "final_output_path": str(out_path),
        }

    def _cmd_project_only(self, args) -> dict:
        if not args.lab and not args.midi:
            raise ValueError("需要 --lab 或 --midi（至少提供一个）")
        if not os.path.exists(args.audio):
            raise ValueError(f"音频文件不存在: {args.audio}")

        default_singer, default_singer_id, default_bs = self._select_vsqx_singer(args.language, "project_only")

        result = self.pipeline.process_project_only(
            wav_path=args.audio,
            lab_path=args.lab,
            output_format=args.format,
            project_title=args.title,
            bpm=args.bpm,
            base_pitch=args.base_pitch,
            f0_method=args.f0_method,
            f0_smooth=not args.no_smooth,
            f0_smooth_window=args.smooth_window,
            use_double_precision=args.double_precision,
            f0_floor=args.f0_floor,
            f0_ceil=args.f0_ceil,
            refine_pitch=args.auto_note_pitch,
            export_pitch_line=args.export_pitch_line,
            vsqx_pitch_smooth_window=args.vsqx_pitch_smooth_window,
            f0_device=args.f0_device,
            crepe_model=args.crepe_model,
            phoneme_mode=args.phoneme_mode,
            midi_path=args.midi,
            lyrics_text=args.lyrics_text,
            vsqx_singer=args.vsqx_singer or default_singer,
            vsqx_singer_id=args.vsqx_singer_id or default_singer_id,
            vsqx_singer_bs=args.vsqx_singer_bs if args.vsqx_singer_bs is not None else default_bs,
            word_phoneme_map=args.word_phoneme_map,
            language=args.language,
            original_text=args.original_text,
            dict_source=args.dict_source,
            ja_devoiced_phoneme=args.ja_devoiced_phoneme,
            fill_short_rests=args.fill_short_rests,
            fill_short_rests_max_length=args.fill_short_rests_max_length,
        )
        if result.get("success"):
            project_path = result.get("project_path") or result.get("output_path")
            print(f"✓ 工程文件生成完成: {project_path}")
            result["final_output_path"] = _finalize_output(project_path, args.output, "工程文件")
        return result

    def _cmd_full(self, args) -> dict:
        text = _read_text(args.text, args.text_file)
        if not text:
            raise ValueError("完整处理需要参考文本，请提供 -t/--text 或 -T/--text-file")
        if not os.path.exists(args.audio):
            raise ValueError(f"音频文件不存在: {args.audio}")

        vsqx_singer, vsqx_singer_id, vsqx_singer_bs = args.vsqx_singer, args.vsqx_singer_id, args.vsqx_singer_bs
        if args.format == "vsqx" and vsqx_singer is None:
            vsqx_singer, vsqx_singer_id, vsqx_singer_bs = self._select_vsqx_singer(args.language, "full")

        audio_file = _FileAdapter(args.audio)
        result = self.pipeline.process_full(
            audio_file, text,
            language=args.language,
            output_format=args.format,
            project_title=args.title,
            bpm=args.bpm,
            base_pitch=args.base_pitch,
            f0_method=args.f0_method,
            f0_smooth=not args.no_smooth,
            f0_smooth_window=args.smooth_window,
            use_double_precision=args.double_precision,
            f0_floor=args.f0_floor,
            f0_ceil=args.f0_ceil,
            refine_pitch=args.auto_note_pitch,
            export_pitch_line=not args.no_pitch_line,
            vsqx_pitch_smooth_window=args.vsqx_pitch_smooth_window,
            f0_device=args.f0_device,
            crepe_model=args.crepe_model,
            aligner_backend=args.aligner_backend,
            aligner_device=args.aligner_device,
            whisperx_model=args.whisperx_model,
            whisperx_batch_size=args.whisperx_batch_size,
            qwen3_batch_size=args.qwen3_batch_size,
            nemo_model=args.nemo_model,
            english_word_align=args.english_word_align,
            ja_disable_katakana=args.ja_disable_katakana,
            vsqx_singer=vsqx_singer or "MIKU_V4_Chinese",
            vsqx_singer_id=vsqx_singer_id or "BNGE7CP7EMTRSNC3",
            vsqx_singer_bs=vsqx_singer_bs if vsqx_singer_bs is not None else 4,
            word_phoneme_map=args.word_phoneme_map,
            dict_source=args.dict_source,
            align_pitch_shift_semitones=args.align_pitch_shift,
            fill_short_rests=args.fill_short_rests,
            fill_short_rests_max_length=args.fill_short_rests_max_length,
        )
        if result.get("success"):
            print(f"✓ 完整处理完成: {result.get('project_path')}")
            result["final_output_path"] = _finalize_output(result.get("project_path"), args.output, "工程文件")
        return result

    def _cmd_subtitle(self, args) -> dict:
        """
        字幕跟读：等价于网页版 /api/subtitle-import/align 背后的
        run_subtitle_align_job()（去掉 Thread/Job 轮询包装，同步执行）。
        对齐固定使用 Qwen3-ForcedAligner，与网页版一致，无 --aligner-backend
        选项。
        """
        if not os.path.exists(args.audio):
            raise ValueError(f"音频文件不存在: {args.audio}")
        if not os.path.exists(args.subtitle_file):
            raise ValueError(f"字幕文件不存在: {args.subtitle_file}")

        ok, msg = subtitle_processor.check_ffmpeg_available()
        if not ok:
            raise ValueError(msg)

        audio_path = Path(args.audio)
        # 非 WAV 输入统一先转成 16k 单声道 WAV，与网页版路由的处理方式
        # 一致（ffmpeg -ss 精确切片依赖 WAV 容器，非 WAV 容器直接切片
        # 会出现时间戳误差）。转换产物放在原音频同目录下，文件名加
        # "_16k" 后缀，避免与原文件重名。
        if audio_path.suffix.lower() != ".wav":
            wav_path = str(audio_path.with_name(audio_path.stem + "_16k.wav"))
            subtitle_processor.extract_audio(str(audio_path), wav_path)
        else:
            wav_path = str(audio_path)

        subtitle_text = Path(args.subtitle_file).read_text(encoding="utf-8-sig")
        audio_duration = subtitle_processor.probe_duration_sec(wav_path)

        fmt, cues = subtitle_import.parse_subtitle_file(
            args.subtitle_file, subtitle_text, audio_duration_sec=audio_duration
        )
        if not cues:
            raise ValueError(f"未能从字幕文件（识别为 {fmt.upper()}）中解析出任何有效条目")

        skip_split_every_n = (
            args.skip_split_every_n if args.skip_split_every_n is not None
            else app_settings.get_subtitle_import_skip_split_every_n()
        )

        print(f"→ 字幕跟读对齐中（{len(cues)} 条字幕，格式 {fmt.upper()}）...")

        def _progress_cb(done, total):
            print(f"  对齐进度: {done}/{total}", end="\r", file=sys.stderr)

        align_result = subtitle_import.align_subtitle_audio(
            wav_path, cues, args.language,
            aligner_device=args.aligner_device,
            english_word_align=args.english_word_align,
            ja_disable_katakana=args.ja_disable_katakana,
            align_pitch_shift_semitones=args.align_pitch_shift,
            audio_duration_sec=audio_duration,
            progress_cb=_progress_cb,
            skip_split_every_n=skip_split_every_n,
        )
        print("", file=sys.stderr)  # 换行，结束上面的 \r 进度行
        if not align_result.get("success"):
            return {"success": False, "error": align_result.get("error", "字幕跟读对齐失败")}

        lab_content = align_result["lab_content"]
        lab_path = str(Path(wav_path).with_suffix(".lab"))
        Path(lab_path).write_text(lab_content, encoding="utf-8")

        if not args.full:
            print(f"✓ 字幕跟读标注完成: {lab_path}")
            result = {
                "success": True,
                "lab_content": lab_content,
                "lab_path": lab_path,
                "audio_duration": align_result.get("audio_duration"),
                "warnings": align_result.get("warnings", []),
            }
            result["final_output_path"] = _finalize_output(lab_path, args.output, "LAB 文件")
            return result

        default_singer, default_singer_id, default_bs = self._select_vsqx_singer(args.language, "full")
        original_text = "\n".join(cue.text for cue in cues if cue.text)

        project_result = self.pipeline.process_project_only(
            wav_path=wav_path, lab_path=lab_path, midi_path=None,
            language=args.language, original_text=original_text,
            output_format=args.format,
            project_title=args.title,
            bpm=args.bpm,
            base_pitch=args.base_pitch,
            f0_method=args.f0_method,
            f0_smooth=not args.no_smooth,
            f0_smooth_window=args.smooth_window,
            use_double_precision=args.double_precision,
            f0_floor=args.f0_floor,
            f0_ceil=args.f0_ceil,
            refine_pitch=args.auto_note_pitch,
            export_pitch_line=not args.no_pitch_line,
            vsqx_pitch_smooth_window=args.vsqx_pitch_smooth_window,
            f0_device=args.f0_device,
            crepe_model=args.crepe_model,
            word_phoneme_map=args.word_phoneme_map,
            dict_source=args.dict_source,
            vsqx_singer=args.vsqx_singer or default_singer,
            vsqx_singer_id=args.vsqx_singer_id or default_singer_id,
            vsqx_singer_bs=args.vsqx_singer_bs if args.vsqx_singer_bs is not None else default_bs,
        )
        if project_result.get("success"):
            project_result.setdefault("lab_content", lab_content)
            project_result["warnings"] = align_result.get("warnings", [])
            print(f"✓ 字幕跟读完整处理完成: {project_result.get('project_path')}")
            project_result["final_output_path"] = _finalize_output(
                project_result.get("project_path"), args.output, "工程文件"
            )
        return project_result

    def _cmd_asr_subtitle(self, args) -> dict:
        """
        字幕识别：等价于网页版 /api/subtitle/recognize + /api/subtitle/export
        背后的逻辑，去掉 Thread/Job 轮询包装、以及"前端持有 entries 再传
        回来导出"的中间步骤——识别完立即导出成文件。【2026-08 起】
        Qwen3-ASR 已迁入 .mfa_env 主进程内本地加载，开箱即用；这里仍先
        检测一次 qwen_asr 包是否已正确安装，未安装时给出明确报错，而不
        是让首次调用时才报出一个不易理解的 ImportError。
        """
        if not os.path.exists(args.audio):
            raise ValueError(f"音频/视频文件不存在: {args.audio}")

        ffmpeg_ok, ffmpeg_msg = subtitle_processor.check_ffmpeg_available()
        if not ffmpeg_ok:
            raise ValueError(ffmpeg_msg)

        from alt_aligners import Qwen3ASRAligner  # 延迟导入，与 app.py 的 subtitle_status() 路由一致
        qwen_ok, qwen_msg = Qwen3ASRAligner.check_available()
        if not qwen_ok:
            raise ValueError(
                f"Qwen3-ASR 不可用: {qwen_msg}（请确认已在 .mfa_env 里执行"
                f" pip install -r requirements.txt 安装 qwen-asr）"
            )

        src_path = Path(args.audio)
        ext = src_path.suffix.lower()
        if ext not in (subtitle_processor.AUDIO_EXTS | subtitle_processor.VIDEO_EXTS):
            raise ValueError(f"不支持的文件类型: {ext or '（无扩展名）'}")

        # 非 WAV 输入统一先转成 16k 单声道 WAV，与网页版路由的处理方式一致
        # （见 _cmd_subtitle 同一处理的注释）。
        if subtitle_processor.is_video_file(str(src_path)) or ext != ".wav":
            wav_path = str(src_path.with_name(src_path.stem + "_16k.wav"))
            subtitle_processor.extract_audio(str(src_path), wav_path)
        else:
            wav_path = str(src_path)

        max_chars = args.max_chars if args.max_chars is not None else subtitle_processor.MAX_SUBTITLE_CHARS
        max_chars = max(8, min(max_chars, 120))
        vad_gap_threshold = max(0.05, min(args.vad_gap_threshold, 5.0))
        batch_size = max(1, min(args.batch_size, 64))

        print(f"→ 字幕识别中（Qwen3-ASR，语言 {args.language}）...")

        def _progress_cb(done, total):
            if total:
                print(f"  识别进度: {done}/{total}", end="\r", file=sys.stderr)

        entries = subtitle_processor.transcribe_to_subtitles(
            wav_path,
            language=args.language,
            device=args.device,
            max_chars=max_chars,
            progress_cb=_progress_cb,
            allow_comma_split=args.allow_comma_split,
            split_at_sentence_end=args.split_at_sentence_end,
            remove_punctuation=args.remove_punctuation,
            close_vad_gaps=args.close_vad_gaps,
            vad_gap_threshold_sec=vad_gap_threshold,
            batch_size=batch_size,
        )
        print("", file=sys.stderr)  # 换行，结束上面的 \r 进度行

        if not entries:
            return {"success": False, "error": "未识别到任何字幕内容（可能是静音音频，或 VAD 未检测到语音片段）"}

        entry_dicts = [e.to_dict() for e in entries]
        content = subtitle_processor.export_subtitles(entry_dicts, args.format)

        out_path = Path(args.output) if args.output else src_path.with_name(src_path.stem + f".{args.format}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")

        print(f"✓ 字幕识别完成: {len(entries)} 条字幕，已写入 {out_path}")
        return {
            "success": True,
            "count": len(entries),
            "format": args.format,
            "entries": entry_dicts,
            "final_output_path": str(out_path),
        }

    def _cmd_dialogue_batch(self, args) -> dict:
        """
        对话文本框批量处理，等价于网页版 /api/dialogue/process 背后的
        pipeline.process_dialogue_batch()，--manifest / --folder 二选一
        提供 boxes（同时提供时 --manifest 优先），其余参数与该方法一一
        对应。

        每个对话框可以是"音频跟读"（上传/指定现成的 wav）或"TTS 跟读"
        （只给文本 + 音色，音频当场合成）之一，--folder 目录扫描只支持
        前者（没有现成音频文件可扫描），两种混用只能通过 --manifest。

        TTS 跟读框的处理方式和网页版 run_dialogue_batch_job() 完全一致：
        不修改 pipeline.process_dialogue_batch() 本身，而是在调用它之前，
        先对每个带 "tts" 信息的框调用 tts_processor.synthesize_and_align()
        合成 + 对齐，把结果回填成 audio_path/lab_path，这样对
        process_dialogue_batch() 来说这些框看起来就是"已经提供了 WAV+LAB"
        的普通音频跟读框，会直接跳过对齐、进入 F0 提取 + 工程文件生成。
        命令行版没有网页版"先点预览再提交复用"这个交互步骤，每次都是
        完整走一遍"合成 → 对齐"，等价于网页版"没有先手动预览、直接点
        开始处理"的那条路径。
        """
        if args.manifest:
            boxes = _load_dialogue_manifest(args.manifest)
        elif args.folder:
            boxes = _scan_dialogue_folder(args.folder)
        else:
            raise ValueError("需要 --manifest 或 --folder（至少提供一个）")

        if not boxes:
            raise ValueError("未能从 --manifest/--folder 中找到任何有效的对话框（至少需要一个音频跟读或 TTS 跟读的条目）")

        for box in boxes:
            if box.get("tts"):
                continue
            if not box.get("audio_path") or not os.path.exists(box["audio_path"]):
                raise ValueError(f"对话框 #{box['index'] + 1} 的音频文件不存在: {box.get('audio_path')}")

        # ── TTS 跟读框预处理：逐个合成 + 对齐，回填 audio_path/lab_path ──
        tts_boxes = [b for b in boxes if b.get("tts")]
        pre_failed = []
        if tts_boxes:
            print(f"→ {len(tts_boxes)} 个 TTS 跟读对话框合成+对齐中...")
            for done, box in enumerate(tts_boxes, start=1):
                tts_info = box["tts"]
                # 文件名带上引擎/来源标签，与网页版（app.py 的
                # run_dialogue_batch_job / /api/tts/process）保持一致的
                # 命名习惯：narrator_id 非空 → "narrator"；否则按 engine
                # 落到 "edgetts"/"qwen3tts"。命令行没有讲述人预设管理，
                # narrator_id 完全由用户手写 manifest 时自行决定是否填写。
                narrator_id = (tts_info.get("narrator_id") or "").strip()
                voice_source_tag = (
                    "narrator" if narrator_id
                    else ("qwen3tts" if (tts_info.get("engine") or tts_processor.DEFAULT_ENGINE) == "qwen3_tts" else "edgetts")
                )
                stem = f"dlg_tts_{voice_source_tag}_{box['index']:03d}_{uuid.uuid4().hex[:6]}"
                tts_result = tts_processor.synthesize_and_align(
                    text=tts_info.get("text") or box.get("text", ""),
                    language=args.language,
                    voice=tts_info.get("voice", ""),
                    engine=tts_info.get("engine") or tts_processor.DEFAULT_ENGINE,
                    work_dir=str(self.pipeline.work_dir),
                    stem=stem,
                    rate=tts_info.get("rate", "+0%"),
                    volume=tts_info.get("volume", "+0%"),
                    pitch=tts_info.get("pitch", "+0Hz"),
                    aligner_device=args.aligner_device,
                    english_word_align=args.english_word_align,
                    ja_disable_katakana=args.ja_disable_katakana,
                    align_pitch_shift_semitones=box.get("align_pitch_shift_semitones", 0.0),
                    qwen3_tts_options=tts_info.get("qwen3_tts_options"),
                )
                print(f"  [{done}/{len(tts_boxes)}] 对话框 #{box['index'] + 1}: "
                      f"{'成功' if tts_result.get('success') else '失败 - ' + str(tts_result.get('error'))}",
                      file=sys.stderr)
                if tts_result.get("success"):
                    box["audio_path"] = tts_result["wav_path"]
                    if not box.get("lab_path") and not box.get("midi_path"):
                        box["lab_path"] = tts_result["lab_path"]
                else:
                    pre_failed.append({
                        "index": box["index"],
                        "status": "failed",
                        "error": tts_result.get("error", "TTS 合成/对齐失败"),
                    })
            boxes = [b for b in boxes if not (b.get("tts") and not b.get("audio_path"))]
            if not boxes:
                return {"success": False, "error": "所有对话框的 TTS 合成/对齐均失败", "boxes": pre_failed}

        default_singer, default_singer_id, default_bs = self._select_vsqx_singer(args.language, "dialogue_batch")
        print(f"→ 对话文本框批量处理中（{len(boxes)} 个对话框）...")

        def _progress_cb(done, total, box_result):
            status = box_result.get("status", "?") if isinstance(box_result, dict) else "?"
            print(f"  [{done}/{total}] 对话框完成，状态: {status}", file=sys.stderr)

        result = self.pipeline.process_dialogue_batch(
            boxes,
            language=args.language,
            output_format=args.format,
            project_title=args.title,
            bpm=args.bpm,
            base_pitch=args.base_pitch,
            f0_method=args.f0_method,
            f0_smooth=not args.no_smooth,
            f0_smooth_window=args.smooth_window,
            use_double_precision=args.double_precision,
            f0_floor=args.f0_floor,
            f0_ceil=args.f0_ceil,
            refine_pitch=args.auto_note_pitch,
            export_pitch_line=not args.no_pitch_line,
            vsqx_pitch_smooth_window=args.vsqx_pitch_smooth_window,
            f0_device=args.f0_device,
            crepe_model=args.crepe_model,
            aligner_backend=args.aligner_backend,
            aligner_device=args.aligner_device,
            whisperx_model=args.whisperx_model,
            whisperx_batch_size=args.whisperx_batch_size,
            qwen3_batch_size=args.qwen3_batch_size,
            nemo_model=args.nemo_model,
            english_word_align=args.english_word_align,
            ja_disable_katakana=args.ja_disable_katakana,
            vsqx_singer=args.vsqx_singer or default_singer,
            vsqx_singer_id=args.vsqx_singer_id or default_singer_id,
            vsqx_singer_bs=args.vsqx_singer_bs if args.vsqx_singer_bs is not None else default_bs,
            word_phoneme_map=args.word_phoneme_map,
            dict_source=args.dict_source,
            processing_mode=args.processing_mode,
            phoneme_mode=args.phoneme_mode,
            ja_devoiced_phoneme=args.ja_devoiced_phoneme,
            fill_short_rests=args.fill_short_rests,
            fill_short_rests_max_length=args.fill_short_rests_max_length,
            progress_cb=_progress_cb,
        )
        if pre_failed:
            result["boxes"] = pre_failed + result.get("boxes", [])
            result["failed_count"] = result.get("failed_count", 0) + len(pre_failed)
        if result.get("success"):
            print(
                f"✓ 对话文本框批量处理完成: {result.get('project_path')} "
                f"（成功 {result.get('processed_count', 0)}，失败 {result.get('failed_count', 0)}，"
                f"跳过 {result.get('skipped_count', 0)}）"
            )
            result["final_output_path"] = _finalize_output(result.get("project_path"), args.output, "工程文件")
        return result

    # ── 词典加载 ─────────────────────────────────────────────────────
    def _cmd_dict_import(self, args) -> dict:
        """
        从 CSV/JSON 文件导入词条到指定词典（等价网页版"单词映射音素词典
        管理"里的"选择 CSV/JSON 文件"导入）。CSV 格式：首行表头
        word,phonemes，其后每行一个词条；JSON 支持扁平 {"WORD":"phones"}
        或 dictionary_manager.export_json() 产出的 {"notation":...,
        "entries":{...}} 两种形状。
        """
        file_path = Path(args.file)
        if not file_path.exists():
            raise ValueError(f"文件不存在: {args.file}")

        raw = file_path.read_text(encoding="utf-8-sig")
        overwrite = not args.no_overwrite

        if file_path.suffix.lower() == ".json":
            payload = json.loads(raw)
            entries = _extract_dict_entries_from_json(payload, args.name)
            if entries is None:
                raise ValueError("JSON 格式不正确，未能解析出词条（既不是扁平 {word:phones}，"
                                  "也不是 {\"notation\":...,\"entries\":{...}} 形状）")
            added, updated = dictionary_manager.bulk_import(
                args.name, entries, overwrite=overwrite, notation=args.notation
            )
        else:
            added, updated = dictionary_manager.import_csv_text(
                args.name, raw, overwrite=overwrite, notation=args.notation
            )

        print(f"✓ 词典 {args.name!r} 导入完成：新增 {added} 条，更新 {updated} 条")
        return {"success": True, "name": args.name, "added": added, "updated": updated}

    def _cmd_dict_list(self, args) -> dict:
        dicts = dictionary_manager.list_dictionaries()
        if not dicts:
            print("（暂无自定义词典）")
        for d in dicts:
            print(f"  {d['name']:<30} {d['notation']:<20} {d['count']} 条")
        return {"success": True, "dictionaries": dicts}

    def _cmd_dict_export(self, args) -> dict:
        if not dictionary_manager.dictionary_exists(args.name):
            raise ValueError(f"词典不存在: {args.name}")

        if args.format == "csv":
            content = dictionary_manager.export_csv(args.name)
        else:
            content = json.dumps(dictionary_manager.export_json(args.name), ensure_ascii=False, indent=2)

        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            print(f"✓ 词典 {args.name!r} 已导出到: {out_path}")
            return {"success": True, "name": args.name, "final_output_path": str(out_path)}
        else:
            print(content)
            return {"success": True, "name": args.name, "content": content}

    def _cmd_dict_edit(self, args) -> dict:
        """
        编辑单个词典：新增/更新或删除单个词条，或新建/删除/改名整个词典。
        与网页版 /api/dictionary 系列路由（POST 创建 / DELETE 删除 /
        PATCH 改名 / entry 子路径 POST 增改词条 / DELETE 删词条）背后是
        同一批 dictionary_manager 函数，行为完全一致。
        """
        action = args.action

        if action == "set":
            if not args.word or args.phonemes is None:
                raise ValueError("action=set 需要 --word 和 --phonemes")
            dictionary_manager.upsert_entry(args.name, args.word, args.phonemes, notation=args.notation)
            print(f"✓ 词典 {args.name!r} 已写入词条: {args.word} → {args.phonemes}")
            return {"success": True, "name": args.name, "action": action, "word": args.word, "phonemes": args.phonemes}

        if action == "remove":
            if not args.word:
                raise ValueError("action=remove 需要 --word")
            removed = dictionary_manager.delete_entry(args.name, args.word)
            if not removed:
                raise ValueError(f"词典 {args.name!r} 中不存在单词: {args.word}（注意按精确大小写匹配）")
            print(f"✓ 已从词典 {args.name!r} 删除词条: {args.word}")
            return {"success": True, "name": args.name, "action": action, "word": args.word}

        if action == "create":
            info = dictionary_manager.create_dictionary(args.name, notation=args.notation or "synthesizerv")
            print(f"✓ 已创建词典 {args.name!r}（记号体系: {info.get('notation')}）")
            return {"success": True, "name": args.name, "action": action, "dictionary": info}

        if action == "delete":
            existed = dictionary_manager.delete_dictionary(args.name)
            if not existed:
                raise ValueError(f"词典不存在: {args.name}")
            print(f"✓ 已删除词典 {args.name!r}")
            return {"success": True, "name": args.name, "action": action}

        if action == "rename":
            if not args.new_name:
                raise ValueError("action=rename 需要 --new-name")
            info = dictionary_manager.rename_dictionary(args.name, args.new_name)
            print(f"✓ 词典 {args.name!r} 已改名为 {args.new_name!r}")
            return {"success": True, "name": args.name, "action": action, "new_name": args.new_name, "dictionary": info}

        raise ValueError(f"未知 action: {action}")  # 理论上不会发生，argparse choices 已经限制取值范围

    # ── 全局设置 ─────────────────────────────────────────────────────
    def _cmd_settings_get(self, args) -> dict:
        settings = app_settings.load_settings()
        print(json.dumps(settings, ensure_ascii=False, indent=2))
        return {"success": True, "settings": settings}

    def _cmd_settings_set(self, args) -> dict:
        """
        更新全局设置，等价网页版"设置"页保存（POST /api/settings）。命令行
        下不会像网页那样自动尝试重启正在运行的 Qwen3/NeMo 微服务——那两个
        微服务是独立进程，命令行这次调用和它们没有进程间关联，重启与否
        请用网页设置页操作，或手动重启对应服务。
        """
        if not args.set:
            raise ValueError("需要至少一个 --set key=value")

        updates = {}
        for item in args.set:
            if "=" not in item:
                raise ValueError(f"--set 参数格式错误（应为 key=value）: {item!r}")
            key, _, raw_value = item.partition("=")
            updates[key.strip()] = _coerce_settings_value(raw_value.strip())

        settings = app_settings.save_settings(updates)
        print(f"✓ 设置已保存（更新了 {len(updates)} 项，未重启 Qwen3/NeMo 微服务，见上方说明）")
        return {"success": True, "settings": settings}
