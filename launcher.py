# -*- coding: utf-8 -*-
"""
launcher.py — Tsubaki 多文件 EXE 启动器

用途
────
把 backend/app.py（.mfa_env）、backend/qwen3_server.py（qwen3_env）、
backend/nemo_server.py（nemo_env）三个跑在各自独立 Python 环境里的服务
拉起来，本身常驻系统托盘，不显示自己的控制台窗口。

这个脚本刻意做得很"薄"：不 import torch / nemo / montreal_forced_aligner
等任何重依赖，只负责进程管理，所以用 PyInstaller 打包出来体积很小、打包
速度很快，和三个后端环境（各自几百 MB～几 GB）完全解耦。

发布目录结构（本脚本假设的布局，与 app.py 里 FRONTEND_DIST 的相对路径
约定保持一致）：

    YourApp/
    ├─ 启动器.exe          ← 本脚本打包后的产物（PyInstaller --onedir）
    ├─ _internal/           ← 同上，PyInstaller onedir 的依赖文件
    ├─ backend/             ← 源码原样拷贝，不冻结
    │   ├─ app.py
    │   ├─ qwen3_server.py
    │   ├─ nemo_server.py
    │   └─ ...
    ├─ frontend/
    │   └─ dist/            ← `npm run build` 产物
    └─ runtime/
       ├─ mfa_env/          ← 便携版 conda 环境（建议用 conda-pack 生成）
       ├─ qwen3_env/
       └─ nemo_env/

打包命令（见同目录 build_launcher.bat）：
    pip install pyinstaller pystray pillow psutil pywebview pythonnet
    pyinstaller --name "Tsubaki启动器" --onedir --noconsole --clean ^
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
  - 点窗口右上角关闭按钮（X）就是彻底退出整个程序：会清理三个后端子进程
    （连同设置页触发 /restart 后产生的孤儿进程）并停掉托盘图标，效果和
    点托盘菜单"退出所有服务"完全一样——两个入口共用同一份清理逻辑，
    互相触发也不会重复执行或报错。
  - webview.start() 必须跑在主线程（部分平台强制要求，Windows 上也建议
    如此），所以主线程留给它，pystray 的 icon.run() 挪到后台线程里跑。

关于控制台显示/隐藏
────────────────────
app_settings.apply_console_visibility() 是用 GetConsoleWindow() 找“自己
所在的”控制台句柄来 Show/Hide 的。所以这里给每个子进程都用
CREATE_NEW_CONSOLE 起一个独立控制台——这样设置页面里“隐藏命令提示符”
开关对三个服务才能各自独立生效，和现在三个人工开 cmd 窗口跑的行为
完全一致，只是变成了由本脚本代劳打开这三个窗口。

关于“下次打开应用不启动 Qwen3-ASR / NeMo Forced Aligner”
──────────────────────────────────────────────────────────
设置页面（SettingsPage.vue）新增了两个独立开关，保存后写入
backend/settings/app_settings.json 里的 skip_start_qwen3_server /
skip_start_nemo_server 两个字段。这两项只在"下一次完整启动应用"时
生效——也就是本脚本每次 start_all() 时会先读一遍这个文件，命中就跳过
对应服务的 _spawn()，不影响当前已经在跑的进程，保存设置本身也不会
关闭或重启任何东西。

关于“退出全部”为什么不能只 terminate 最初的 PID
────────────────────────────────────────────────
qwen3_server.py / nemo_server.py 的 /restart 路由是“先关端口，再用
subprocess.Popen 拉一个全新进程，旧进程 os._exit(0)”，重启之后的 PID
已经不是本脚本一开始记下来的那个了。所以退出时除了 terminate 已知的
Popen 对象，还要按命令行特征（脚本文件名）再扫一遍进程列表，把设置页
触发重启后产生的“孤儿”进程也清理掉，否则每次在设置页点过一次“应用
更改”，退出按钮就会漏杀一个进程，只能靠任务管理器强杀。

关于命令行调用模式（`启动器.exe cmd ...`）
──────────────────────────────────────────
参考 VVTALK 项目 reliance/interface/commandline.py 的模式：用
sys.argv[1] 是否等于 "cmd" 来判断这次启动到底是"正常跑起来"（托盘 +
pywebview 原生窗口 + 三个后端服务）还是"命令行调用一次就退"。

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

HOST = "127.0.0.1"
MAIN_PORT = 5000

# 三个后端服务：脚本名 + 各自独立 Python 环境的目录名（对应 runtime/ 下）+
# 可选的"跳过启动"设置项键名（对应 app_settings.py 里的 DEFAULT_SETTINGS）。
# app.py 是主服务，没有跳过选项，永远启动。
SERVICES: List[Dict[str, Optional[str]]] = [
    {"name": "app",   "script": "app.py",          "env": "mfa_env",   "skip_key": None},
    {"name": "qwen3", "script": "qwen3_server.py", "env": "qwen3_env", "skip_key": "skip_start_qwen3_server"},
    {"name": "nemo",  "script": "nemo_server.py",  "env": "nemo_env",  "skip_key": "skip_start_nemo_server"},
]

# 命令行一次性调用模式（`启动器.exe cmd <operation> ...`）固定转发给
# app.py（mfa_env）——commandline.py 的 CmdUI 就是挂在 app.py 这个进程
# 里的（backend/commandline.py + backend/app.py 的 __main__ 分流逻辑），
# 三个后端服务里只有它认识 "cmd" 这个子命令，qwen3_server.py /
# nemo_server.py 是纯常驻 HTTP 微服务，没有对应的命令行入口。
CMD_TRIGGER = "cmd"
CMD_SERVICE = SERVICES[0]  # {"name": "app", "script": "app.py", "env": "mfa_env", ...}

# 与 app_settings.py 里 SETTINGS_PATH 的约定保持一致：设置文件固定放在
# backend/settings/app_settings.json。launcher.py 在拉起子进程之前，
# app.py 还没启动，没有 HTTP 接口可用，所以这里直接读文件，不发请求。
SETTINGS_PATH = BACKEND_DIR / "settings" / "app_settings.json"

CREATE_NEW_CONSOLE = 0x00000010  # subprocess.CREATE_NEW_CONSOLE，仅 Windows 有意义

# --noconsole 打包后 sys.stdout / sys.stderr 是 None，不能用 StreamHandler，
# 只写文件，方便出问题时排查。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [launcher] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
logger = logging.getLogger("launcher")

_procs: List[subprocess.Popen] = []


# ────────────────────────────────────────────────────────────────
# 启动子进程
# ────────────────────────────────────────────────────────────────

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
    不开 pywebview 原生窗口、不拉起 qwen3/nemo 两个微服务，只把整条
    命令行原样转发给 runtime/mfa_env/python.exe backend/app.py，等它
    跑完，把它的 stdout/stderr/退出码原样透传回来，然后本进程退出。

    参考 VVTALK 项目 reliance/interface/commandline.py 的 "argv[1]=='cmd'
    则不启动 GUI，转而走命令行分支" 的判断模式；这里额外多一层"转发"，
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

    logger.info("启动 %s: %s %s", service["name"], py, script.name)
    env = os.environ.copy()
    if service["name"] == "app":
        # app.py 自己 main() 里会自动 webbrowser.open() 一次；由 launcher 起时
        # 界面已经交给 pywebview 原生窗口负责，这里关掉它，避免多弹出一个
        # 浏览器标签页。不影响单独 `python app.py` 调试时的默认行为。
        env["SVS_SKIP_AUTO_BROWSER"] = "1"
    try:
        return subprocess.Popen(
            [str(py), str(script)],
            cwd=str(BACKEND_DIR),
            creationflags=CREATE_NEW_CONSOLE,
            close_fds=True,
            env=env,
        )
    except Exception as e:
        logger.error("启动 %s 失败: %s", service["name"], e)
        return None


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
    统一的退出清理逻辑：杀掉三个后端子进程 + 停掉托盘图标。

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
    icon = pystray.Icon("tsubaki_launcher", _make_icon_image(), "Tsubaki 对齐工具", menu)
    _tray_icon = icon
    icon.run()


def main() -> None:
    global _main_window

    start_all()

    if not _wait_for_backend_ready(timeout=30.0):
        logger.warning("等待 app.py 就绪超时（30s），仍然打开窗口，页面会自行重试连接。")

    _main_window = webview.create_window(
        title="Tsubaki 对齐工具",
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
        #   Tsubaki启动器.exe cmd mfa-only -a in.wav -t "参考文本" -o out.lab
        #   Tsubaki启动器.exe cmd full -a in.wav -t "参考文本" -f sv -o out.svp
        # 完整参数列表见 backend/commandline.py，或运行:
        #   Tsubaki启动器.exe cmd <operation> --help
        sys.exit(_run_cmd_mode(sys.argv))
    main()
