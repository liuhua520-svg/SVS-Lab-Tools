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
    pip install pyinstaller pystray pillow psutil
    pyinstaller --name "Tsubaki启动器" --onedir --noconsole --clean launcher.py

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
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

try:
    import psutil
except ImportError:
    psutil = None

from PIL import Image, ImageDraw
import pystray

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
    try:
        return subprocess.Popen(
            [str(py), str(script)],
            cwd=str(BACKEND_DIR),
            creationflags=CREATE_NEW_CONSOLE,
            close_fds=True,
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


def _open_browser_delayed() -> None:
    # 三个服务里 app.py 起 Flask 通常很快，但如果磁盘/CPU 繁忙可能要几秒，
    # 这里给点余量；具体是否加载完模型不影响打开页面，页面本身会自己轮询状态。
    time.sleep(3)
    webbrowser.open(f"http://{HOST}:{MAIN_PORT}")


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
    logger.info("正在退出所有服务...")
    _kill_tracked()
    time.sleep(0.5)
    _kill_by_cmdline()
    icon.stop()


def open_ui_action(icon, item) -> None:
    webbrowser.open(f"http://{HOST}:{MAIN_PORT}")


# ────────────────────────────────────────────────────────────────
# 托盘图标
# ────────────────────────────────────────────────────────────────

def _make_icon_image() -> Image.Image:
    img = Image.new("RGB", (64, 64), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=(216, 90, 48))
    return img


def run_tray() -> None:
    menu = pystray.Menu(
        pystray.MenuItem("打开界面", open_ui_action, default=True),
        pystray.MenuItem("退出所有服务", quit_all),
    )
    icon = pystray.Icon("tsubaki_launcher", _make_icon_image(), "Tsubaki 对齐工具", menu)
    icon.run()


def main() -> None:
    start_all()
    threading.Thread(target=_open_browser_delayed, daemon=True).start()
    run_tray()  # 阻塞，直到用户点“退出所有服务”


if __name__ == "__main__":
    main()
