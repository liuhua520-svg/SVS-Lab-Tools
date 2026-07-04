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
    main()
