# ffmpeg_locator.py
# -*- coding: utf-8 -*-
"""
ffmpeg / ffprobe 定位。

查找顺序：
  1. 系统环境变量 PATH（全局安装的 ffmpeg，优先使用）；
  2. backend/ffmpeg 目录（本程序自带/用户自行放入的便携版），依次尝试
        backend/ffmpeg/
        backend/ffmpeg/bin/
        backend/ffmpeg/<任意一级子目录>/bin/    例：解压官方 zip 后的
                                                 ffmpeg-7.0-essentials_build/bin
        backend/ffmpeg/<任意一级子目录>/
     PyInstaller 打包后，同样会在 exe 所在目录（及其 backend 子目录）下找 ffmpeg 目录；
  3. 当前工作目录下的同名文件（旧版行为，保留兼容）。

在 backend/ffmpeg 里找到时，会把该目录加到本进程 PATH 最前面（只加一次），
这样依赖 PATH 的第三方调用——例如进程内运行的 WhisperX 的
whisperx.load_audio()——也能直接找到 ffmpeg，无需用户改系统环境变量。
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

#: 便携版 ffmpeg 所在的目录名（位于 backend/ 下）
LOCAL_FFMPEG_DIRNAME = "ffmpeg"


def _roots() -> List[Path]:
    """可能存放 ffmpeg 目录的上级目录（去重、保持顺序）。"""
    roots: List[Path] = [Path(__file__).resolve().parent]
    if getattr(sys, "frozen", False):          # PyInstaller 打包
        exe_dir = Path(sys.executable).resolve().parent
        roots += [exe_dir, exe_dir / "backend"]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots += [Path(meipass), Path(meipass) / "backend"]
    seen, out = set(), []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def local_ffmpeg_dirs() -> List[Path]:
    """backend/ffmpeg 下所有值得检查的目录（按优先级）。"""
    dirs: List[Path] = []
    for root in _roots():
        base = root / LOCAL_FFMPEG_DIRNAME
        if not base.is_dir():
            continue
        dirs += [base, base / "bin"]
        try:
            subs = sorted(p for p in base.iterdir() if p.is_dir() and p.name != "bin")
        except OSError:
            subs = []
        for sub in subs:
            dirs += [sub / "bin", sub]
    return [d for d in dirs if d.is_dir()]


def find_local_executable(name: str) -> Optional[str]:
    """只在 backend/ffmpeg 里找（Windows 下自动尝试 .exe 等 PATHEXT）。"""
    for d in local_ffmpeg_dirs():
        found = shutil.which(name, path=str(d))
        if found:
            return str(Path(found).resolve())
    return None


def find_executable(name: str) -> Optional[str]:
    """PATH → backend/ffmpeg → 当前目录，返回可执行文件的路径或 None。"""
    found = shutil.which(name)
    if found:
        return found
    found = find_local_executable(name)
    if found:
        return found
    for c in (Path(name), Path(f"{name}.exe")):
        if c.is_file():
            return str(c.resolve())
    return None


def ensure_ffmpeg_on_path() -> Optional[str]:
    """
    若全局 PATH 里没有 ffmpeg，但 backend/ffmpeg 里有，则把那个目录加进本进程
    PATH（幂等）。返回加入 PATH 的目录；无需处理 / 没找到时返回 None。
    """
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return None
    added: Optional[str] = None
    for name in ("ffmpeg", "ffprobe"):
        if shutil.which(name):
            continue
        local = find_local_executable(name)
        if not local:
            continue
        d = str(Path(local).parent)
        parts = os.environ.get("PATH", "").split(os.pathsep)
        if d not in parts:
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            logger.info("[ffmpeg] 全局 PATH 中没有 %s，已使用 backend/ffmpeg 中的版本: %s", name, d)
        added = d
    return added


def describe_source(path: str) -> str:
    """可执行文件来自哪里：'backend/ffmpeg'（便携版）或 '系统 PATH'/'当前目录'。"""
    try:
        p = Path(path).resolve()
    except OSError:
        return "未知"
    for d in local_ffmpeg_dirs():
        try:
            p.relative_to(d.resolve())
            return "backend/ffmpeg"
        except ValueError:
            continue
    sys_hit = shutil.which(p.stem if p.suffix.lower() == ".exe" else p.name,
                           path=os.environ.get("PATH", ""))
    if sys_hit and Path(sys_hit).resolve() == p:
        return "系统 PATH"
    return "当前目录"
