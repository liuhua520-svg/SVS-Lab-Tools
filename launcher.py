# -*- coding: utf-8 -*-
"""
launcher.py — SVS Lab Tools 多文件 EXE 启动器

用途
────
把 backend/app.py（mfa_env）、backend/qwen3_server.py（qwen3_env）、
backend/qwen3tts_server.py（qwen3tts_env）、backend/nemo_server.py（nemo_env）
四个跑在各自独立 Python 环境里的服务拉起来，本身常驻系统托盘，不显示
自己的控制台窗口。

这个脚本刻意做得很"薄"：不 import torch / nemo / montreal_forced_aligner
等任何重依赖，只负责进程管理，所以用 PyInstaller 打包出来体积很小、打包
速度很快，和四个后端环境（各自几百 MB～几 GB）完全解耦。

发布目录结构（本脚本假设的布局，与 app.py 里 FRONTEND_DIST 的相对路径
约定保持一致）：

    YourApp/
    ├─ 启动器.exe          ← 本脚本打包后的产物（PyInstaller --onedir）
    ├─ _internal/           ← 同上，PyInstaller onedir 的依赖文件
    ├─ backend/             ← 源码原样拷贝，不冻结
    │   ├─ app.py
    │   ├─ qwen3_server.py
    │   ├─ qwen3tts_server.py
    │   ├─ nemo_server.py
    │   └─ ...
    ├─ frontend/
    │   └─ dist/            ← `npm run build` 产物
    └─ runtime/
       ├─ mfa_env/          ← 便携版 conda 环境（建议用 conda-pack 生成）
       ├─ qwen3_env/
       ├─ qwen3tts_env/
       └─ nemo_env/

打包命令（见同目录 build_launcher.bat）：
    pip install pyinstaller pystray pillow psutil pywebview pythonnet
    pyinstaller --name "SVS Lab Tools" --onedir --noconsole --clean ^
        --hidden-import=clr_loader --hidden-import=pythonnet launcher.py

关于原生应用窗口（不再打开系统浏览器）
──────────────────────────────────────
本脚本用 pywebview 起一个原生窗口加载 http://127.0.0.1:5000，取代了旧版
webbrowser.open() 在系统默认浏览器里开标签页的做法。相应地：

  - app.py 自己 main() 里原本也会 Thread 一个 open_browser() 自动开浏览器
    （方便单独用 `python app.py` 调试）。launcher.py 通过给 app.py 子进程
    注入环境变量 SVS_SKIP_AUTO_BROWSER=1 来关掉这个自动打开——否则每次
    启动会同时弹出"原生窗口 + 浏览器标签页"两个界面。单独调试 app.py 时
    不设这个环境变量，行为不受影响。
  - 点窗口右上角关闭按钮（X）就是彻底退出整个程序：会清理四个后端子进程
    （连同设置页触发 /restart 后产生的孤儿进程）并停掉托盘图标，效果和
    点托盘菜单"退出所有服务"完全一样——两个入口共用同一份清理逻辑，
    互相触发也不会重复执行或报错。
  - webview.start() 必须跑在主线程（部分平台强制要求，Windows 上也建议
    如此），所以主线程留给它，pystray 的 icon.run() 挪到后台线程里跑。

关于控制台窗口（不再显示，日志改写文件）
────────────────────────────────────────
【2026-08 变更】早期版本给每个子进程用 CREATE_NEW_CONSOLE 起独立控制台
窗口，再靠 app_settings.apply_console_visibility()（GetConsoleWindow() +
ShowWindow(SW_HIDE)）实现设置页面里的"隐藏命令提示符窗口"开关。但在把
"默认终端应用"设为 Windows Terminal 的 Windows 11 系统上（22H2 起的
系统默认值），CREATE_NEW_CONSOLE 起的控制台会被 Windows Terminal 接管、
包一层独立的顶层窗口，GetConsoleWindow() 隐藏的是被接管前的底层句柄，
对任务栏上真正显示的 Windows Terminal 窗口没有任何效果，"隐藏"开关在
这类系统上形同虚设。

现在改为：_spawn() 用 CREATE_NO_WINDOW 拉起全部四个子进程（app.py /
qwen3_server.py / qwen3tts_server.py / nemo_server.py），从系统层面
直接不分配任何控制台窗口——不管当前系统默认终端是 conhost 还是 Windows
Terminal，任务栏上都不会出现任何图标，一次性根治，不再依赖"事后隐藏"
这种取决于宿主实现细节的做法。相应地，四个进程各自的 stdout/stderr 被
重定向到 logs/<service_name>.log（见 _open_service_log()），要看某个
服务的运行日志，直接打开对应的日志文件即可。app_settings.py 里的
hide_console_window 设置项和 apply_console_visibility() 因此变为历史
遗留——只为兼容旧版本写过的设置文件而保留字段，不再实际影响任何窗口。

关于“下次打开应用不启动 Qwen3-ASR / Qwen3-TTS / NeMo Forced Aligner”
────────────────────────────────────────────────────────────────────
设置页面（SettingsPage.vue）新增了三个独立开关，保存后写入
backend/settings/app_settings.json 里的 skip_start_qwen3_server /
skip_start_qwen3tts_server / skip_start_nemo_server 三个字段。这些
选项只在"下一次完整启动应用"时生效——也就是本脚本每次 start_all() 时
会先读一遍这个文件，命中就跳过对应服务的 _spawn()，不影响当前已经在
跑的进程，保存设置本身也不会关闭或重启任何东西。

关于“退出全部”为什么不能只 terminate 最初的 PID
────────────────────────────────────────────────
qwen3_server.py / qwen3tts_server.py / nemo_server.py 的 /restart 路由
是“先关端口，再用 subprocess.Popen 拉一个全新进程，旧进程 os._exit(0)”，
重启之后的 PID 已经不是本脚本一开始记下来的那个了。所以退出时除了
terminate 已知的 Popen 对象，还要按命令行特征（脚本文件名）再扫一遍
进程列表，把设置页触发重启后产生的“孤儿”进程也清理掉，否则每次在
设置页点过一次“应用更改”，退出按钮就会漏杀一个进程，只能靠任务管理器
强杀。

关于命令行调用模式（`启动器.exe cmd ...`）
──────────────────────────────────────────
用 sys.argv[1] 是否等于 "cmd" 来判断这次启动到底是"正常跑起来"
（托盘 +pywebview 原生窗口 + 四个后端服务）还是"命令行调用一次就退"。

本脚本自己不实现具体的标注提取/音高提取/工程文件生成逻辑——那些依赖
mfa_env 里的一整套重依赖（MFA/torch/pyworld 等），不适合塞进这个刻意
做得很薄的 exe 里。真正的实现在 backend/commandline.py（CmdUI 类），
挂在 backend/app.py 的 `if __name__ == "__main__":` 分支里（判断逻辑
与本文件的 _is_cmd_invocation() 完全一致）。本脚本检测到 cmd 模式后，
只做一件事：把整条命令行原样转发给
`runtime/mfa_env/python.exe backend/app.py cmd ...` 子进程，透传它的
stdout/stderr/退出码，不起托盘、不开原生窗口、不拉 qwen3/nemo 两个
微服务。用法及完整参数见 backend/commandline.py 顶部说明，或运行
`启动器.exe cmd <operation> --help`。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

try:
    import psutil
except ImportError:
    psutil = None

from PIL import Image, ImageDraw
import pystray
import webview

# ────────────────────────────────────────────────────────────────
# 路径解析
# ────────────────────────────────────────────────────────────────

def _app_root() -> Path:
    """
    发布包的顶层目录，backend/ frontend/ runtime/ 都是它的子目录。

    - 被 PyInstaller 打包成 exe 运行时：sys.executable 就是 启动器.exe
      自身的路径，它和 backend/ frontend/ runtime/ 平级放置。
    - 直接用 `python launcher.py` 调试时：用脚本自身所在目录代替。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = _app_root()
BACKEND_DIR = APP_ROOT / "backend"
RUNTIME_DIR = APP_ROOT / "runtime"
LOG_PATH = APP_ROOT / "launcher.log"
LOGS_DIR = APP_ROOT / "logs"  # 四个后端子进程各自的 stdout/stderr 日志文件存放目录

HOST = "127.0.0.1"
MAIN_PORT = 5000

# 四个后端服务：脚本名 + 各自独立 Python 环境的目录名（对应 runtime/ 下）+
# 可选的"跳过启动"设置项键名（对应 app_settings.py 里的 DEFAULT_SETTINGS）。
# app.py 是主服务，没有跳过选项，永远启动；其余三个是可各自独立跳过的
# 微服务（qwen3 / qwen3tts / nemo）。
SERVICES: List[Dict[str, Optional[str]]] = [
    {"name": "app",      "script": "app.py",           "env": "mfa_env",      "skip_key": None},
    {"name": "qwen3",    "script": "qwen3_server.py",  "env": "qwen3_env",    "skip_key": "skip_start_qwen3_server"},
    {"name": "qwen3tts", "script": "qwen3tts_server.py", "env": "qwen3tts_env", "skip_key": "skip_start_qwen3tts_server"},
    {"name": "nemo",     "script": "nemo_server.py",   "env": "nemo_env",     "skip_key": "skip_start_nemo_server"},
]

# 命令行一次性调用模式（`启动器.exe cmd <operation> ...`）固定转发给
# app.py（mfa_env）——commandline.py 的 CmdUI 就是挂在 app.py 这个进程
# 里的（backend/commandline.py + backend/app.py 的 __main__ 分流逻辑），
# 四个后端服务里只有它认识 "cmd" 这个子命令，qwen3_server.py /
# qwen3tts_server.py / nemo_server.py 是纯常驻 HTTP 微服务，没有对应的
# 命令行入口。
CMD_TRIGGER = "cmd"
CMD_SERVICE = SERVICES[0]  # {"name": "app", "script": "app.py", "env": "mfa_env", ...}

# 与 app_settings.py 里 SETTINGS_PATH 的约定保持一致：设置文件固定放在
# backend/settings/app_settings.json。launcher.py 在拉起子进程之前，
# app.py 还没启动，没有 HTTP 接口可用，所以这里直接读文件，不发请求。
SETTINGS_PATH = BACKEND_DIR / "settings" / "app_settings.json"

# 【2026-08 变更】原来用 CREATE_NEW_CONSOLE 给每个子进程起独立控制台窗口，
# 配合 app_settings.apply_console_visibility() 的 ShowWindow(SW_HIDE) 来实现
# 设置页面里的"隐藏命令提示符窗口"开关。但在把"默认终端应用"设为 Windows
# Terminal 的 Windows 11 系统上（22H2 起的系统默认值），CREATE_NEW_CONSOLE
# 创建的控制台会被 Windows Terminal 接管、包一层独立的 WindowsTerminal.exe
# 顶层窗口——GetConsoleWindow() 拿到的是被接管前的底层句柄，隐藏它对任务栏
# 上真正显示的 Windows Terminal 窗口毫无效果，导致"隐藏"开关形同虚设。
#
# 现在改用 CREATE_NO_WINDOW：从系统层面直接不为子进程分配任何控制台窗口，
# 不管当前系统默认终端是 conhost 还是 Windows Terminal，都不会有任何窗口
# 或任务栏图标出现，一次性根治，不再依赖"事后隐藏"这种取决于宿主实现的
# 做法。代价是子进程没有可继承的标准输出——所以 _spawn() 改为把每个子
# 进程的 stdout/stderr 重定向到 logs/<name>.log 文件（见 _spawn()），
# hide_console_window 这个设置项和 apply_console_visibility() 相应地
# 变为历史遗留、不再有实际效果，仅为兼容旧设置文件保留字段。
CREATE_NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW，仅 Windows 有意义

# --noconsole 打包后 sys.stdout / sys.stderr 是 None，不能用 StreamHandler，
# 只写文件，方便出问题时排查。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [launcher] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
logger = logging.getLogger("launcher")

LOGS_DIR.mkdir(parents=True, exist_ok=True)

_procs: List[subprocess.Popen] = []


# ────────────────────────────────────────────────────────────────
# 启动子进程
# ────────────────────────────────────────────────────────────────

def _open_service_log(service_name: str):
    """
    为某个后端子进程打开（追加模式）它自己的日志文件 logs/<service_name>.log，
    返回一个已打开的文件对象，供 _spawn() 作为子进程的 stdout/stderr 传入。

    用追加而不是覆盖：同一个服务多次重启（包括设置页触发的 /restart，见
    qwen3_server.py / qwen3tts_server.py / nemo_server.py 的 /restart 路由）
    产生的日志都能在同一个文件里连续看到，不会互相覆盖，方便回溯问题。
    文件不设自动轮转/清理——如果日志文件长期运行后体积过大，手动删除
    对应文件即可，下次启动会自动重新创建。

    注意：这里的 encoding="utf-8" 只在本进程（launcher.py）自己往这个
    文件对象写内容时才生效——但实际上 _spawn() 从不用它写任何内容，只是
    把它的操作系统级文件句柄（.fileno()）传给子进程当 stdout/stderr，用完
    立刻关闭（见 _spawn() 的 finally 块）。子进程通过这个继承来的句柄
    写入时，用的是子进程自己 io.TextIOWrapper 的编码逻辑，不受这里
    encoding 参数的约束——真正保证子进程写入内容是 UTF-8 编码的，是
    _spawn() 里给子进程环境变量设置的 PYTHONIOENCODING=utf-8，两者必须
    同时成立（父进程这边的 encoding 决定"万一父进程要读/写这个文件该按
    什么编码"，子进程那边的 PYTHONIOENCODING 决定"子进程写进去的字节实际
    是什么编码"）才能保证日志文件从头到尾都是合法 UTF-8，不会中途变成
    GBK 或者乱码。
    """
    log_path = LOGS_DIR / f"{service_name}.log"
    return open(log_path, "a", encoding="utf-8", errors="replace")


def _env_python(env_name: str) -> Optional[Path]:
    """
    在 runtime/<env_name>/ 下寻找 python.exe。
    优先按 conda 环境布局（python.exe 在根目录），
    再兜底按普通 venv 布局（Scripts/python.exe）。
    """
    candidate = RUNTIME_DIR / env_name / "python.exe"
    if candidate.exists():
        return candidate
    candidate2 = RUNTIME_DIR / env_name / "Scripts" / "python.exe"
    if candidate2.exists():
        return candidate2
    return None


def _cmd_eprint(message: str) -> None:
    """
    命令行模式下打印错误信息给用户看，同时兼顾 --noconsole 打包出来的
    exe：正常从终端调用 `exe cmd ...` 时 sys.stderr 是那个终端的句柄，
    print() 没问题；但 PyInstaller --noconsole/--windowed 在某些版本/
    某些启动路径下会把 sys.stdout / sys.stderr 设为 None（本文件顶部
    logging 配置那里也提到了同样的原因，所以日志改成只写文件）。这里
    做一次判空，None 时退化为只写日志文件，不让一次简单的错误提示反而
    因为 print(..., file=None) 抛 AttributeError 把真正的错误信息盖掉。
    """
    if sys.stderr is not None:
        try:
            print(message, file=sys.stderr)
            return
        except Exception:
            pass
    logger.error(message)


def _is_cmd_invocation(argv=None) -> bool:
    """
    判断规则必须和 backend/commandline.py 里 is_cmd_mode() 的判断逻辑
    完全一致（都是"argv[1] 精确等于 'cmd'"）——这里先判断一次决定要不要
    转发，转发过去之后 app.py 那边再判断一次决定要不要走 CmdUI，两处
    任何一处放宽/收紧匹配规则都要同步改另一处，否则会出现"已经被
    launcher 转发过来了，但 app.py 自己又判断不是 cmd 模式，转而去启动
    整个 HTTP 服务"这类不一致。
    """
    argv = sys.argv if argv is None else argv
    return len(argv) > 1 and argv[1] == CMD_TRIGGER


def _run_cmd_mode(argv: List[str]) -> int:
    """
    `启动器.exe cmd <operation> ...` 命令行一次性调用模式：不起托盘、
    不开 pywebview 原生窗口、不拉起 qwen3/qwen3tts/nemo 三个微服务，
    只把整条命令行原样转发给 runtime/mfa_env/python.exe backend/app.py，
    等它跑完，把它的 stdout/stderr/退出码原样透传回来，然后本进程退出。

    "argv[1]=='cmd' 则不启动 GUI，转而走命令行分支" 的判断模式；这里额外多一层"转发"，
    是因为本项目的命令行实现（CmdUI）依赖 mfa_env 里的一整套重依赖
    （pipeline.py / tsubaki_processor.py 等），不适合塞进 launcher.py
    自己这个刻意做得很薄、不引入重依赖的 exe 里（见文件顶部说明）。

    转发细节：
      - argv[0]（启动器.exe 自身路径）替换成 backend/app.py 的路径，
        argv[1:]（"cmd" 及后面所有参数）原样保留，拼成
        [python.exe, app.py, "cmd", ...] 交给 mfa_env 的 Python 执行——
        与 backend/commandline.is_cmd_mode() 里"argv[1] 是否等于 cmd"
        的判断逻辑天然对齐，app.py 收到后会自己再判断一次走 CmdUI 分支。
      - 不用 CREATE_NEW_CONSOLE：命令行调用场景下用户就是从现有的
        cmd/PowerShell 窗口敲的这条命令，期望输出直接打印在当前窗口里，
        而不是像托盘模式那样每个服务弹一个新控制台。--noconsole 打包出的
        启动器.exe 自身没有控制台子系统，但从已有终端窗口调用时，子进程
        默认继承的标准输入/输出/错误句柄仍然是那个终端的，stdout/stderr
        直接透传，不需要额外配置。
      - 不注入 SVS_SKIP_AUTO_BROWSER：那个环境变量是给"常驻 HTTP 服务 +
        pywebview 原生窗口"场景用的，commandline.py 的 CmdUI 分支根本
        不会走到 main()/app.run()，不受影响，但保留默认环境变量传递
        （env=None 即继承当前进程环境）更简单、也没有副作用。
    """
    py = _env_python(str(CMD_SERVICE["env"]))
    script = BACKEND_DIR / str(CMD_SERVICE["script"])

    if py is None:
        _cmd_eprint(
            f"[错误] 找不到命令行功能依赖的 Python 运行时 "
            f"(runtime/{CMD_SERVICE['env']}/python.exe 不存在)。\n"
            f"请确认本 exe 与 backend/ frontend/ runtime/ 三个目录放在同一层级下，"
            f"且 runtime/{CMD_SERVICE['env']}/ 已经用 pack_runtime.bat 打包好。"
        )
        return 1
    if not script.exists():
        _cmd_eprint(f"[错误] 找不到 {script}，命令行功能不可用。")
        return 1

    forwarded = [str(py), str(script), *argv[1:]]  # argv[1:] = ["cmd", operation, ...]
    logger.info("命令行模式，转发到: %s", forwarded)

    try:
        result = subprocess.run(forwarded, cwd=str(BACKEND_DIR))
        return result.returncode
    except Exception as e:
        logger.error("命令行转发失败: %s", e)
        _cmd_eprint(f"[错误] 命令行转发失败: {e}")
        return 1


def _spawn(service: Dict[str, str]) -> Optional[subprocess.Popen]:
    py = _env_python(service["env"])
    script = BACKEND_DIR / service["script"]

    if py is None:
        logger.error(
            "找不到 %s 的 Python 环境（runtime/%s/python.exe 不存在），跳过启动 %s",
            service["name"], service["env"], service["script"],
        )
        return None
    if not script.exists():
        logger.error("找不到脚本 %s，跳过启动", script)
        return None

    log_file = _open_service_log(service["name"])
    logger.info(
        "启动 %s: %s %s（不创建控制台窗口，输出写入 %s）",
        service["name"], py, script.name, log_file.name,
    )
    env = os.environ.copy()
    # 【2026-08 变更，修复 UnicodeEncodeError】四个后端脚本的启动横幅/日志里
    # 都带 emoji（🚀 📍 📂 等），之前用 CREATE_NEW_CONSOLE 时这些字符能正常
    # 打印，是因为 Windows 控制台设备本身默认按 UTF-8 编码处理输出（较新
    # Python 版本的默认行为，见 PEP 528）；但磁盘文件/管道这类"非控制台"
    # 目标走的是系统区域编码——中文 Windows 上就是 GBK。现在 stdout/stderr
    # 直接重定向到磁盘文件，Python 的 io.TextIOWrapper 会退回用
    # locale.getpreferredencoding() 探测编码，GBK 编不了 emoji 这类字符，
    # 会直接抛 UnicodeEncodeError 且不会自动回退，导致 print() 那一行直接
    # 把整个进程炸掉（app.py 表现为主服务启动到一半就崩溃退出；
    # qwen3_server.py 等用 logging 模块的则是 StreamHandler 默认绑定
    # sys.stderr，而 sys.stderr 的默认错误处理器是 backslashreplace 而非
    # strict，不会抛异常，但日志里看到的是转义后的 "\U0001f680" 字面量
    # 文本，不是真正的图案）。
    # 用 PYTHONIOENCODING=utf-8 强制子进程的 sys.stdout/sys.stderr 用
    # UTF-8 编码写入，和 _open_service_log() 里父进程 open(..., 
    # encoding="utf-8") 打开日志文件时用的编码保持一致，从根上解决，不用
    # 逐个改四个脚本里的每一处 print()/logging 调用。这个环境变量会随
    # env=env 一起被子进程继承；三个微服务 /restart 时的自重启用的是
    # `subprocess.Popen([python] + sys.argv, ...)`（不传 env，默认继承
    # 当前进程也就是本进程的环境），所以重启后依然带着这个设置，不会失效。
    env["PYTHONIOENCODING"] = "utf-8"
    if service["name"] == "app":
        # app.py 自己 main() 里会自动 webbrowser.open() 一次；由 launcher 起时
        # 界面已经交给 pywebview 原生窗口负责，这里关掉它，避免多弹出一个
        # 浏览器标签页。不影响单独 `python app.py` 调试时的默认行为。
        env["SVS_SKIP_AUTO_BROWSER"] = "1"
    try:
        return subprocess.Popen(
            [str(py), str(script)],
            cwd=str(BACKEND_DIR),
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
        )
    except Exception as e:
        logger.error("启动 %s 失败: %s", service["name"], e)
        return None
    finally:
        # subprocess.Popen 内部会把这个句柄 dup 给子进程，父进程这边的文件
        # 对象用不上了，关掉即可——不影响子进程那边已经继承的写入端。
        log_file.close()


def _load_startup_settings() -> Dict[str, object]:
    """
    直接读取 backend/settings/app_settings.json 里"下次启动是否跳过"
    这两个字段。读不到文件或字段缺失，一律按 False（即"正常启动"）处理，
    保证首次运行、设置文件损坏等情况下不会意外少启动服务。
    """
    try:
        if SETTINGS_PATH.exists():
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(
            "读取设置文件失败（%s），本次按默认设置启动全部服务: %s", SETTINGS_PATH, e
        )
    return {}


def start_all() -> None:
    logger.info("应用根目录: %s", APP_ROOT)
    settings = _load_startup_settings()

    for service in SERVICES:
        skip_key = service.get("skip_key")
        if skip_key and bool(settings.get(skip_key, False)):
            logger.info(
                "设置里已勾选“下次打开应用不启动 %s”，本次跳过启动。",
                service["script"],
            )
            continue

        proc = _spawn(service)
        if proc is not None:
            _procs.append(proc)


def _wait_for_backend_ready(timeout: float = 30.0) -> bool:
    """
    轮询 app.py 的 /api/health，等主服务真正能响应 HTTP 请求了再去创建
    原生窗口——避免窗口一开出来就是"连接被拒绝"的空白/报错页面。

    超时后仍然返回 False 而不是抛异常：就算 30 秒内没等到（比如机器很慢），
    也继续把窗口开出来，前端页面本身会自己重试请求，不阻塞用户看到界面。
    """
    url = f"http://{HOST}:{MAIN_PORT}/api/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


# 原生窗口对象 / 托盘图标对象，供各回调函数引用。
# 都在 main() 里创建后赋值，此后只读，不重新创建。
_main_window: Optional["webview.Window"] = None
_tray_icon: Optional["pystray.Icon"] = None

_shutdown_started = False
_shutdown_lock = threading.Lock()


def _shutdown() -> None:
    """
    统一的退出清理逻辑：杀掉四个后端子进程 + 停掉托盘图标。

    两个入口都会调用它——窗口被关闭（events.closed）和托盘"退出所有
    服务"菜单——所以用一个标志位加锁保证只真正执行一次：quit_all() 里
    destroy() 窗口会连带触发 events.closed 再调一次 _shutdown()，此时
    应该直接跳过，否则会重复 terminate 已经不存在的进程（虽然无害，但
    没必要）、以及对已经 stop() 过的托盘图标再 stop() 一次。
    """
    global _shutdown_started
    with _shutdown_lock:
        if _shutdown_started:
            return
        _shutdown_started = True

    logger.info("正在退出所有服务...")
    _kill_tracked()
    time.sleep(0.5)
    _kill_by_cmdline()
    if _tray_icon is not None:
        try:
            _tray_icon.stop()
        except Exception as e:
            logger.warning("停止托盘图标失败: %s", e)


def _on_window_closed() -> None:
    """窗口被关闭（点 X）之后触发：彻底退出整个程序。"""
    _shutdown()


# ────────────────────────────────────────────────────────────────
# 退出逻辑
# ────────────────────────────────────────────────────────────────

def _kill_tracked() -> None:
    for proc in _procs:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


def _kill_by_cmdline() -> None:
    """
    按命令行里是否包含某个服务脚本文件名来扫描并杀掉所有匹配进程，
    用来兜底清理 /restart 产生的、_procs 里已经跟丢的孤儿进程。
    """
    if psutil is None:
        logger.warning(
            "未安装 psutil，无法扫描孤儿进程；如果某个服务被设置页重启过，"
            "其重启后的新进程可能无法通过退出按钮杀掉。"
        )
        return

    targets = {s["script"] for s in SERVICES}
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = p.info.get("cmdline") or []
            if any(any(t in str(part) for t in targets) for part in cmdline):
                logger.info("终止残留进程 PID=%s: %s", p.info["pid"], cmdline)
                p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def quit_all(icon: "pystray.Icon", _item=None) -> None:
    _shutdown()
    # 销毁窗口会让阻塞在主线程里的 webview.start() 返回，main() 才能走完，
    # 整个 launcher 进程随之退出；不销毁的话，托盘线程停了但主线程还卡在
    # webview 的 GUI 循环里，进程不会真正退出。这一步会连带触发窗口的
    # events.closed（进而再调一次 _shutdown()），已经用标志位挡掉了。
    if _main_window is not None:
        try:
            _main_window.destroy()
        except Exception as e:
            logger.warning("销毁窗口失败: %s", e)


def open_ui_action(icon, item) -> None:
    if _main_window is None:
        return
    try:
        _main_window.show()
        _main_window.restore()
    except Exception as e:
        logger.warning("恢复窗口显示失败: %s", e)


# ────────────────────────────────────────────────────────────────
# 托盘图标
# ────────────────────────────────────────────────────────────────

def _make_icon_image() -> Image.Image:
    img = Image.new("RGB", (64, 64), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=(216, 90, 48))
    return img


def run_tray() -> None:
    global _tray_icon
    menu = pystray.Menu(
        pystray.MenuItem("打开界面", open_ui_action, default=True),
        pystray.MenuItem("退出所有服务", quit_all),
    )
    icon = pystray.Icon("SVS_Lab_Tools_launcher", _make_icon_image(), "SVS Lab Tools", menu)
    _tray_icon = icon
    icon.run()


def _patch_download_starting_to_hide_default_flyout() -> None:
    """
    【2026-08 变更，第二次修复：右上角下载通知浮层仍然弹出】

    第一版方案（追加挂一个"第二订阅者"到 CoreWebView2.DownloadStarting 上，
    只设 args.Handled，不动另存为对话框逻辑）实测无效，问题出在时序，不是
    api 用错了：

    - pywebview 自带的 on_download_starting 是*同步阻塞*调用
      dialog.ShowDialog(self.form)（模态对话框，用户不点确定/取消它不返回），
      而且它自己没有调用 args.GetDeferral()。
    - WebView2 官方文档明确写了：宿主如果不显式挂起 deferral，
      CoreWebView2 会在"事件处理完成的那一刻"就去读 args 上的属性——而
      "事件处理完成"在 .NET 多播委托的语义里，指的是*这一次事件分发挂的
      所有订阅者都跑完*。理论上我们追加的第二个订阅者应该能在同一次分发
      里赶上，但实测（见用户反馈）没生效，说明这条链路在 pywebview 当前
      版本的实现细节下不可靠，不能继续赌时序。

    改成更直接、不依赖时序假设的办法：不追加订阅者，而是在
    webview.create_window() 之前，直接把
    webview.platforms.edgechromium.EdgeChrome.on_download_starting 这个
    *类方法本身*替换成我们自己的版本——完整保留原版的另存为对话框逻辑
    （在原有实现基础上原样复用，行为不变），只在它决定"不取消、允许下载"
    的分支末尾，多做一件事：args.Handled = True。

    这样从头到尾只有一个订阅者在处理这个事件，不存在"两个处理器谁先谁后
    执行""追加订阅是否被正确叠加"这些不确定性——WebView2 在这个唯一的
    处理器返回后读到的 Handled 就是 True，不会再弹它自己的默认下载通知
    浮层（右上角那个黑色面板）。

    必须在 webview.create_window() 之前调用：EdgeChrome.__init__ 里是
    `self.webview.CoreWebView2InitializationCompleted += self.on_webview_ready`，
    而 on_webview_ready 内部又是 `sender.CoreWebView2.DownloadStarting +=
    self.on_download_starting`——这两处的 `self.on_download_starting` 都是
    在窗口真正创建、事件真正触发时才去类上查找方法，所以只要我们在窗口
    创建之前把类属性替换掉，后续所有窗口实例查到的都会是新版本，不需要
    在每个窗口创建后再单独处理一次。

    只在 Windows（EdgeChromium 后端）上有意义；其他平台 import 这个模块
    会失败（缺 pythonnet/WebView2 相关依赖），直接跳过，不影响
    macOS/Linux 下的运行。
    """
    if sys.platform != "win32":
        return

    try:
        from webview.platforms import edgechromium

        original = edgechromium.EdgeChrome.on_download_starting

        def _patched_on_download_starting(self, sender, args):
            # 完整保留原版逻辑（另存为对话框 / 取消判断），不改变现有
            # 行为，只在下面追加 Handled=True。
            original(self, sender, args)
            if not args.Cancel:
                try:
                    args.Handled = True
                except Exception as e:
                    edgechromium.logger.warning("设置 DownloadStarting.Handled 失败: %s", e)

        edgechromium.EdgeChrome.on_download_starting = _patched_on_download_starting
        logger.info("已替换 EdgeChrome.on_download_starting，抑制 WebView2 默认下载通知浮层。")
    except Exception as e:
        # 这属于"锦上添花"的体验优化，不是核心功能——就算因为 pywebview
        # 内部实现在未来版本变了导致这里失败，也不应该阻塞应用正常启动，
        # 顶多是右上角的系统下载浮层又出现了（回退到打补丁前的行为）。
        logger.warning("替换 on_download_starting 失败，将回退为 WebView2 默认下载 UI: %s", e)


def main() -> None:
    global _main_window

    start_all()

    if not _wait_for_backend_ready(timeout=30.0):
        logger.warning("等待 app.py 就绪超时（30s），仍然打开窗口，页面会自行重试连接。")

    # 【2026-08 变更，修复下载无反应】pywebview 默认关闭浏览器标准的文件
    # 下载能力（ALLOW_DOWNLOADS 默认 False）——前端 `<a download>` /
    # blob: URL 那套触发下载的写法，在普通浏览器里会自动存进"下载"目录，
    # 但在 pywebview 包的这个原生窗口里，底层 WebView2 控件收到下载请求后
    # 因为这个开关关着直接静默取消（EdgeChromium 后端 on_download_starting
    # 里 `if not ALLOW_DOWNLOADS: args.Cancel = True; return`），前端代码
    # 那边看起来就是"点了按钮没反应，也不报错"——因为请求根本没到达浏览器的
    # 下载流程，是被 pywebview 自己在原生层拦掉的，前端 JS 完全感知不到。
    #
    # 打开这个开关后，Windows 上 WebView2 每次触发下载都会弹出一个真正的
    # 系统级"另存为"对话框（WinForms.SaveFileDialog），用户自己选保存位置
    # 和文件名，这正是"所有下载请求都变成另存为文件"这个需求要的效果——
    # 不需要改前端任何一行下载相关代码（<a download>、blob: URL 这些写法
    # 本身没有问题，只是之前从来没被允许触发）。
    #
    # 必须在 webview.start() 之前设置（webview.create_window() 之前更保险，
    # 官方文档明确要求"Application settings must be set before invoking
    # webview.start() to have an effect"）。
    webview.settings["ALLOW_DOWNLOADS"] = True

    # 见 _patch_download_starting_to_hide_default_flyout() 顶部注释：必须
    # 在 create_window() 之前打这个补丁——EdgeChrome 实例是在 create_window
    # 内部才真正创建的，补丁要在那之前就位，后续窗口查找
    # on_download_starting 方法时才能拿到替换后的版本。
    _patch_download_starting_to_hide_default_flyout()

    _main_window = webview.create_window(
        title="SVS Lab Tools",
        url=f"http://{HOST}:{MAIN_PORT}",
        width=1280,
        height=800,
        min_size=(1000, 650),
    )
    _main_window.events.closed += _on_window_closed

    # pystray 的 icon.run() 和 webview.start() 都是阻塞调用；webview 要求
    # 跑在主线程，所以托盘挪到后台线程执行。
    threading.Thread(target=run_tray, daemon=True).start()

    webview.start()  # 阻塞主线程，直到窗口被 quit_all() 里的 destroy() 关掉
    logger.info("主窗口已关闭，launcher 进程退出。")


if __name__ == "__main__":
    if _is_cmd_invocation():
        # 命令行一次性调用：转发给 backend/app.py（mfa_env）执行，不启动
        # 托盘/原生窗口/qwen3/nemo 微服务，跑完就退出。用法例如:
        #   SVS Lab Tools.exe cmd mfa-only -a in.wav -t "参考文本" -o out.lab
        #   SVS Lab Tools.exe cmd full -a in.wav -t "参考文本" -f sv -o out.svp
        # 完整参数列表见 backend/commandline.py，或运行:
        #   SVS Lab Tools.exe cmd <operation> --help
        sys.exit(_run_cmd_mode(sys.argv))
    main()
