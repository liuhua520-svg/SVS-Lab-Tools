# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

class MFAChecker:
    # 【稳健性修复】缓存最近一次成功的 "mfa version" 检测结果。
    # 同一台机器上跑对齐任务时（不管是 MFA 还是 Qwen3），CPU/磁盘 IO 被占满，
    # 会导致冷启动 import montreal_forced_aligner + kalpy 的子进程偶尔超过
    # 超时时间，从而把"系统繁忙"误判成"MFA 未安装"——这正是
    # "有时检测不到 MFA，刷新页面才恢复正常" 的根因（刷新只是又重试了一次，
    # 刚好赶上系统不那么忙）。
    _status_cache_lock = threading.Lock()
    _last_good_mfa_check: Optional[Tuple[bool, str, float]] = None  # (ok, msg, timestamp)
    _MFA_CHECK_CACHE_TTL = 120.0  # 秒：在这个窗口内允许复用上一次的成功结果

    # MFA 3.3.9 模型映射：语言代码 -> {"dictionary": ..., "acoustic": ...}
    LANGUAGE_MODELS: Dict[str, Dict[str, str]] = {
        "cmn": {
            "dictionary": "mandarin_china_mfa",
            "acoustic": "mandarin_mfa",
        },
        "zh": {
            "dictionary": "mandarin_china_mfa",
            "acoustic": "mandarin_mfa",
        },
        "eng": {
            "dictionary": "english_us_mfa",
            "acoustic": "english_mfa",
        },
        "en": {
            "dictionary": "english_us_mfa",
            "acoustic": "english_mfa",
        },
        "jpn": {
            "dictionary": "japanese_mfa",
            "acoustic": "japanese_mfa",
        },
        "ja": {
            "dictionary": "japanese_mfa",
            "acoustic": "japanese_mfa",
        },
        "kor": {
            "dictionary": "korean_mfa",
            "acoustic": "korean_mfa",
        },
        "ko": {
            "dictionary": "korean_mfa",
            "acoustic": "korean_mfa",
        },
        "yue": {
            "dictionary": "mandarin_china_mfa",
            "acoustic": "mandarin_mfa",
        }
    }

    # ===== 输出格式映射：语言 → 转换目标 =====
    PHONEME_OUTPUT_FORMAT: Dict[str, str] = {
        'en': 'arpabet',     # English → ARPABET
        'eng': 'arpabet',
        'ja': 'romaji',      # Japanese → ROMAJI
        'jpn': 'romaji',
        'zh': 'pinyin',      # Chinese → Pinyin (no conversion needed)
        'cmn': 'pinyin',
        'yue': 'jyutping',   # Cantonese → Jyutping
        'ko': 'hangul',      # Korean → Hangul Jamo (no conversion needed)
        'kor': 'hangul',
    }

    @staticmethod
    def project_root() -> Path:
        return Path(__file__).resolve().parent.parent

    @staticmethod
    def env_dir() -> Path:
        env_dir = os.environ.get("MFA_ENV_DIR")
        if env_dir and Path(env_dir).exists():
            return Path(env_dir)

        # 旧版打包方式：隐藏目录直接放在应用根目录下（.mfa_env）。
        local_prefix = MFAChecker.project_root() / ".mfa_env"
        if local_prefix.exists():
            return local_prefix

        # 【2026-08 补充】现行 launcher.py 的打包布局是
        # <APP_ROOT>/runtime/mfa_env（不带前导点、统一放在 runtime/ 下，
        # 与 whisperx_env / qwen3tts_env / nemo_env / kaldi_env 平级），而不是
        # 上面那种旧版 .mfa_env 布局；launcher.py 本身也没有设置
        # MFA_ENV_DIR 环境变量。之前这里漏掉这条查找路径，全靠下面
        # sys.prefix 兜底"凑巧"算对——因为 backend/app.py 本来就是被
        # runtime/mfa_env/python.exe 启动的，sys.prefix 自然就是这个目录。
        # 但这只是巧合，换一种启动方式（比如未来某天从别的解释器 import
        # 这个模块、或者需要在不同子进程里显式引用 mfa_env 路径时）就会
        # 失效，所以还是补上显式的查找路径，不再单纯依赖巧合。
        runtime_prefix = MFAChecker.project_root() / "runtime" / ".mfa_env"
        if runtime_prefix.exists():
            return runtime_prefix

        return Path(sys.prefix)

    @staticmethod
    def kaldi_env_dir() -> Optional[Path]:
        """
        独立的 kaldi 环境（setup.bat/setup.sh 用 `conda create -p .kaldi_env
        -c conda-forge kaldi` 创建，或現行打包流程里的
        runtime/kaldi_env）。_kalpy 是纯粹的 Python 绑定，运行时依赖这里面
        的 kaldi 动态库（Windows 下在 Library\\bin，Linux/Mac 下在 lib），
        必须和 mfa_env 的 PATH/LD_LIBRARY_PATH 一起传给子进程，否则
        `import _kalpy` 会因为找不到底层 DLL/so 而报 ModuleNotFoundError
        （表现具有迷惑性，看起来像没装包，实际是 native 依赖没装进 PATH）。

        【2026-08 排查记录】实际出现过"看起来像没装包"的另一种变体：不是
        DLL 加载失败，而是这个函数直接返回 None（因为 .kaldi_env 和
        runtime/kaldi_env 都没找到——例如打包用的是 runtime/kaldi_env 这种
        新布局，而这里当时只认旧的 .kaldi_env），导致 build_env() 里
        KALDI_ROOT / KALDI_ENV_DIR / PYTHONPATH 全部没有被设置，子进程连
        "去哪儿找 _kalpy" 都不知道，报错是最上层的
        `ModuleNotFoundError: No module named '_kalpy'`——比 DLL 加载失败
        还要更前一步。两种失败现象都要看这个函数有没有正确定位到目录。
        """
        env_dir = os.environ.get("KALDI_ENV_DIR")
        if env_dir and Path(env_dir).exists():
            return Path(env_dir)

        # 旧版打包方式：隐藏目录直接放在应用根目录下（.kaldi_env）。
        local_prefix = MFAChecker.project_root() / ".kaldi_env"
        if local_prefix.exists():
            return local_prefix

        # 【2026-08 补充】现行打包布局：<APP_ROOT>/runtime/kaldi_env（不带
        # 前导点，与 runtime/mfa_env 等平级，与 launcher.py 的 RUNTIME_DIR
        # 约定一致）。这是本函数之前唯一缺失的一条查找路径，也是
        # "ModuleNotFoundError: No module named '_kalpy'" 这个问题的根因：
        # 走到这里之前的两条路径都找不到目录时，之前会直接返回 None，
        # 导致上面 build_env() 完全没有机会设置 KALDI_ROOT / PYTHONPATH，
        # 子进程运行 montreal_forced_aligner 时自然找不到 _kalpy。
        runtime_prefix = MFAChecker.project_root() / "runtime" / ".kaldi_env"
        if runtime_prefix.exists():
            return runtime_prefix

        return None

    @staticmethod
    def kaldi_site_packages_dir() -> Optional[Path]:
        """
        `.kaldi_env` 下 kalpy（`conda install -c conda-forge kalpy`）安装的
        site-packages 目录。Windows 下是 `<prefix>\\Lib\\site-packages`，
        Linux/Mac 下是 `<prefix>/lib/python3.X/site-packages`。
        _kalpy/kalpy 装在这里，而不是 .mfa_env，所以 .mfa_env 的 Python 要
        import 它，必须显式把这个目录加进 sys.path（子进程通过 PYTHONPATH
        环境变量传递），否则不管 PATH/KALDI_ROOT 怎么设，Python 的 import
        机制根本不知道去哪儿找这个包，直接报 ModuleNotFoundError。
        """
        kaldi_env_dir = MFAChecker.kaldi_env_dir()
        if not kaldi_env_dir:
            return None

        win_site_packages = kaldi_env_dir / "Lib" / "site-packages"
        if win_site_packages.exists():
            return win_site_packages

        lib_dir = kaldi_env_dir / "lib"
        if lib_dir.exists():
            for child in lib_dir.iterdir():
                if child.is_dir() and child.name.startswith("python"):
                    candidate = child / "site-packages"
                    if candidate.exists():
                        return candidate

        return None

    # 【新增】只暴露 kalpy 本体的 shim 目录名，跟 .kaldi_env 平级，不跟
    # .mfa_env / .kaldi_env 本身混在一起，方便识别和清理。
    _KALPY_SHIM_DIRNAME = ".kalpy_shim"
    _kalpy_shim_lock = threading.Lock()

    # 【新增，2026-08-16 pynini 缺失修复】montreal_forced_aligner.data 在
    # import 时会无条件 `import pynini`（跟 kalpy 一样是纯 conda-forge
    # 二进制包，PyPI 上只有 manylinux wheel，Windows 下 pip 装不出来），
    # 之前 setup.bat 只把 kaldi/kalpy 装进了 .kaldi_env，漏掉了 pynini，
    # 表现为 "ModuleNotFoundError: No module named 'pynini'"。
    # pynini 在 site-packages 下对应的产物（同样参考 conda-forge 官方
    # feedstock 和实际打包案例确认）：
    #   - `_pynini*.pyd` / `_pynini*.so`：编译好的扩展模块本体
    #   - `pynini/` ：纯 Python 包装层
    #   - `_pywrapfst*.pyd` / `_pywrapfst*.so` + `pywrapfst/`：pynini 依赖的
    #     OpenFst Python 绑定底层模块，pynini 内部会 import 它
    # 这些同样需要收进 shim，否则即使 conda 装进了 .kaldi_env，PYTHONPATH
    # 里精简过的 shim 目录还是看不到它们。
    _PYNINI_SHIM_MODULE_GLOBS = ("_pynini*.pyd", "_pynini*.so", "_pywrapfst*.pyd", "_pywrapfst*.so")
    _PYNINI_SHIM_PACKAGE_DIRS = ("pynini", "pywrapfst")

    @staticmethod
    def _kalpy_shim_dir() -> Optional[Path]:
        """
        【问题背景，2026-08-16 numpy 2.x ABI 崩溃排查后新增】

        之前 build_kaldi_subprocess_env() / check_kalpy() 直接把整个
        `.kaldi_env\\Lib\\site-packages` 塞进 PYTHONPATH / sys.path 最前面。
        CPython 组装 sys.path 的顺序固定是：
            [脚本目录] -> PYTHONPATH 各目录（原样顺序）-> 标准库 -> 解释器自己的
            site-packages
        也就是说无论把 `.kaldi_env` 的 site-packages 放在 PYTHONPATH 字符串里
        的前面还是后面，它在最终 sys.path 里永远排在 `.mfa_env` 自己的
        site-packages **之前**——这不是本项目代码能左右的顺序，是 CPython
        启动时构建 sys.path 的固定规则。

        而 `.kaldi_env` 是用
            conda create -p .kaldi_env -c conda-forge python=3.10 kaldi
            conda install -p .kaldi_env -c conda-forge kalpy
        装的，kalpy 在 conda-forge 上的依赖会带进一份独立的、跟 `.mfa_env`
        里 `pip install numpy==1.26.4` 完全无关的 numpy（实测版本随
        conda-forge 当前索引浮动，见 2026-08-16 现场 pip list：
        `.kaldi_env` 是 numpy 2.4.6，`.mfa_env` 是 numpy 1.26.4）。
        同理 `.kaldi_env` 还带了自己独立的 scipy / librosa / pandas /
        scikit-learn 等一整套包。

        后果：`.mfa_env\\python.exe` 子进程一旦被塞了整个
        `.kaldi_env\\Lib\\site-packages` 进 PYTHONPATH，`import numpy` 拿到的
        就是排在最前面的 `.kaldi_env` 那份 numpy 2.x，而不是 `.mfa_env` 里
        `pip install -r requirements.txt` 明确锁定、且 torch==2.3.1+cpu /
        torchaudio==2.3.1+cpu 编译时实际链接的 numpy==1.26.4——numpy 1.x/2.x
        C-API 不兼容，torchaudio 在 import 时执行的
        `torch.finfo(torch.float).eps` 立刻触发 ABI 崩溃：
            "A module that was compiled using NumPy 1.x cannot be run in
             NumPy 2.2.6 ..."
        这个 torchaudio 是被 sitecustomize.py 里
        `_patch_speechbrain_lazy_module_windows_path_bug()` 的
        `from speechbrain.utils import importutils` 间接触发导入的（speechbrain
        核心链路会 import torchaudio）。sitecustomize.py 在这一步直接崩溃，
        导致整个 `.mfa_env\\python.exe` 解释器启动中断——所以紧跟着看到的
        `ImportError: DLL load failed while importing _kalpy` 只是解释器已经
        在崩溃过程中的连带失败，并不是 _kalpy 本身的 DLL 路径问题重新出现
        （那部分逻辑正常，只是根本没机会跑到）。

        修复思路：不能再把 `.kaldi_env` 整个 site-packages 目录暴露给
        `.mfa_env` 子进程，只能精确地"只借用" `_kalpy` 扩展模块本体和纯
        Python 的 `kalpy` 包这两样东西，绝不能让 `.kaldi_env` 自带的 numpy /
        scipy / librosa 等有机会排在 `.mfa_env` 自己那份前面。

        做法：在 `.kaldi_env` 同级新建一个很薄的 shim 目录
        （`<project_root>/.kalpy_shim`），里面只放：
          - `_kalpy*.pyd` / `_kalpy*.so`（编译好的扩展模块本体）
          - `kalpy/` 包目录（纯 Python 部分）
        用符号链接优先（Windows 需要开发者模式或管理员权限创建符号链接，
        因此这里做了失败回退），失败则退化为直接复制文件（kalpy 体积不大，
        复制一次性成本可接受，且只在 shim 目录不存在或内容跟源目录不一致时
        才会重新生成，不会每次子进程启动都重新复制）。

        这样 PYTHONPATH 里塞的是这个只含 `_kalpy` / `kalpy` 的瘦身目录，而
        不是整个 `.kaldi_env` site-packages，`.kaldi_env` 自带的 numpy/scipy
        等就不会再出现在 `.mfa_env` 子进程的 sys.path 里，`.mfa_env` 自己
        pip 装的 numpy==1.26.4 会被正常找到并使用。

        【2026-08-16 二次扩展】同样的道理适用于 pynini/pywrapfst——它们和
        kalpy 一样只能通过 conda-forge 装进 .kaldi_env，且同样必须只借用
        编译产物本体和纯 Python 包装层，不能把 .kaldi_env 整个
        site-packages（连带那份不兼容的 numpy 2.x）暴露出去。因此这里把
        pynini/pywrapfst 的对应文件也一并纳入同一个 shim 目录，跟 kalpy
        共用一次 "先在临时目录建好、再原子改名换入" 的构建流程。
        pynini 缺失是非致命的——找不到就跳过，不影响 kalpy 部分照常工作
        （kalpy 是 mfa align 的强依赖，pynini 目前只在部分语言的 G2P/
        文本规整路径用到，缺了只应该在触发那部分逻辑时才报错）。
        """
        kaldi_site_packages = MFAChecker.kaldi_site_packages_dir()
        if not kaldi_site_packages:
            return None

        src_pyd = None
        for pattern in ("_kalpy*.pyd", "_kalpy*.so"):
            matches = sorted(kaldi_site_packages.glob(pattern))
            if matches:
                src_pyd = matches[0]
                break
        src_kalpy_pkg = kaldi_site_packages / "kalpy"
        if src_pyd is None or not src_kalpy_pkg.exists():
            # 找不到 _kalpy 扩展模块或 kalpy 纯 Python 包，说明 kalpy 没有
            # 正确安装到 .kaldi_env，没法搭 shim，直接返回 None，调用方会
            # 退回旧行为（同时也会在 check_kalpy 里如实报错）。
            return None

        # pynini/pywrapfst 是可选的：找不到就跳过，不阻塞 kalpy 部分的 shim
        # 构建（这部分是 mfa align 的强依赖，优先级更高）。
        pynini_extra_files: List[Path] = []
        for pattern in MFAChecker._PYNINI_SHIM_MODULE_GLOBS:
            pynini_extra_files.extend(sorted(kaldi_site_packages.glob(pattern)))
        pynini_extra_dirs: List[Path] = []
        for pkg_name in MFAChecker._PYNINI_SHIM_PACKAGE_DIRS:
            pkg_path = kaldi_site_packages / pkg_name
            if pkg_path.exists():
                pynini_extra_dirs.append(pkg_path)

        shim_dir = MFAChecker.project_root() / MFAChecker._KALPY_SHIM_DIRNAME
        shim_pyd = shim_dir / src_pyd.name
        shim_kalpy_pkg = shim_dir / "kalpy"

        def _needs_rebuild() -> bool:
            if not shim_pyd.exists() or not shim_kalpy_pkg.exists():
                return True
            try:
                # 源文件比 shim 新（比如 kalpy 被升级过），需要重建。
                if src_pyd.stat().st_mtime > shim_pyd.stat().st_mtime:
                    return True
            except OSError:
                return True
            # pynini/pywrapfst：只要源文件存在但 shim 里缺失（比如 shim 是
            # 修复前的旧版本建的，只有 kalpy 那部分），也需要重建一次，把
            # 新增的 pynini 部分补齐。
            for f in pynini_extra_files:
                if not (shim_dir / f.name).exists():
                    return True
            for d in pynini_extra_dirs:
                if not (shim_dir / d.name).exists():
                    return True
            return False

        # 【修复，2026-08-16 第二轮】首次触发 shim 构建时曾观察到一个可能的
        # 竞态窗口：Flask 开发服务器是多线程的，健康检查/状态轮询（如
        # /api/aligner/status）和真正的 `mfa align` 任务都可能各自在独立的
        # 后台线程里调用 build_kaldi_subprocess_env() -> _kalpy_shim_dir()。
        # 如果机器上第一次触发 shim 构建时恰好有两个线程同时判断
        # "_needs_rebuild() == True" 并同时执行 mkdir / unlink / symlink_to，
        # 这些操作本身不是原子的，交错执行可能让某个线程看到的 shim 目录
        # 处于"文件已删除、符号链接还没建好"的中间态——如果这个瞬间正好有
        # 第三个线程（比如刚提交的 mfa align 子进程）读到这个不完整状态，
        # 就会出现 "_kalpy.pyd 存在但内容/链接不完整" 从而 DLL 加载失败，
        # 且这个失败具有偶发性、不稳定复现的特征（跟观察到的现象吻合：
        # 单独手动跑诊断脚本时 shim 已经建好，import 就成功了）。
        #
        # 用进程内锁 + "先在临时目录建好整个 shim，再整体原子改名换入正式
        # 位置" 两个手段消除这个窗口：
        #   1. 锁保证同一进程内不会有两个线程同时执行构建/重建逻辑；
        #   2. 真正对外可见的 shim_dir 只会通过一次 os.replace()（Windows
        #      上对同卷目录是原子操作）从"不存在"直接变为"完全建好"，不存在
        #      任何线程能看到只建了一半的正式目录。
        # 多进程（比如两个独立 python.exe 子进程同时是第一次触发构建）的
        # 极端情况这个进程内锁盖不到，但 Flask 主进程通常是当前机器上唯一
        # 会调用这个函数的长期驻留进程，实践中已经能覆盖绝大多数场景；
        # 真正的 `mfa align` 子进程本身不会再次调用本函数（它是被 Popen
        # 启动的独立解释器，直接使用已经通过环境变量/PYTHONPATH 传入的
        # 现成 shim 路径，不会自己再构建一次）。
        with MFAChecker._kalpy_shim_lock:
            if _needs_rebuild():
                import shutil
                import tempfile

                shim_dir.mkdir(parents=True, exist_ok=True)
                staging_dir = Path(
                    tempfile.mkdtemp(prefix=".kalpy_shim_build_", dir=str(shim_dir.parent))
                )
                try:
                    staging_pyd = staging_dir / src_pyd.name
                    staging_kalpy_pkg = staging_dir / "kalpy"
                    try:
                        staging_pyd.symlink_to(src_pyd)
                        staging_kalpy_pkg.symlink_to(src_kalpy_pkg, target_is_directory=True)
                    except (OSError, NotImplementedError):
                        # 没有创建符号链接的权限（Windows 默认非管理员/未
                        # 开发者模式）时退化为复制。kalpy 纯 Python 包体积
                        # 不大，一次性复制成本可接受；_kalpy 扩展模块同理。
                        shutil.copy2(src_pyd, staging_pyd)
                        shutil.copytree(src_kalpy_pkg, staging_kalpy_pkg)

                    # pynini/pywrapfst：可选，逐个尝试符号链接、失败则复制，
                    # 单个文件/目录失败不影响其它文件，也不影响 kalpy 主体
                    # （已经在上面 staging 完成），只跳过失败的那一项。
                    for f in pynini_extra_files:
                        staging_f = staging_dir / f.name
                        try:
                            staging_f.symlink_to(f)
                        except (OSError, NotImplementedError):
                            try:
                                shutil.copy2(f, staging_f)
                            except OSError:
                                pass
                    for d in pynini_extra_dirs:
                        staging_d = staging_dir / d.name
                        try:
                            staging_d.symlink_to(d, target_is_directory=True)
                        except (OSError, NotImplementedError):
                            try:
                                shutil.copytree(d, staging_d)
                            except OSError:
                                pass

                    # 先清掉旧的正式目录（如果存在），再把建好的临时目录
                    # 整体改名换上——os.replace 在同一卷内对目录是原子的，
                    # 不会有"正式目录存在但内容为空/不完整"的中间状态。
                    if shim_dir.exists():
                        shutil.rmtree(shim_dir, ignore_errors=True)
                    os.replace(str(staging_dir), str(shim_dir))
                except Exception:
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    raise

        return shim_dir

    @staticmethod
    def build_kaldi_subprocess_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """
        给所有会触发 `import montreal_forced_aligner` / `import _kalpy` 的
        子进程（mfa version / mfa model download / mfa align 等）统一拼装
        环境变量。

        【关键点 1】kalpy 是 `conda install -c conda-forge kalpy` 装进
        .kaldi_env 的（不是 .mfa_env，也不是 pip），所以 .mfa_env 的 Python
        运行子进程时默认根本看不到这个包——必须把 .kaldi_env 的
        site-packages 目录塞进 PYTHONPATH，否则无论 PATH/KALDI_ROOT 怎么
        设都还是 ModuleNotFoundError（这是比"DLL 加载失败"更前一步的问题：
        Python 连模块在哪儿都不知道）。
        【关键点 2】kalpy-kaldi 运行时通过 KALDI_ROOT 环境变量定位 Kaldi
        共享库，而不是泛泛地扫 PATH；Windows 下扩展模块 (.pyd) 加载依赖
        DLL 时也不再默认搜索 PATH，所以 KALDI_ROOT 依然要设。PATH/
        LD_LIBRARY_PATH 一并设置，作为其它原生依赖的兜底。
        """
        # 【2026-08-16 第三轮修复】在拼装子进程环境变量之前，先确保
        # .mfa_env 里实际生效的 sitecustomize.py 跟 backend/
        # mfa_env_sitecustomize.py 源文件内容一致——否则即使这里把
        # KALDI_ROOT/PYTHONPATH 等环境变量传得再正确，子进程里真正调用
        # os.add_dll_directory() 的仍然是旧版 sitecustomize.py（如果部署
        # 之后源文件又被修改过、但从没重新跑过 setup.bat 或手动重新复制），
        # 旧版本里可能还没有当前这一版的修复，导致环境变量传对了、DLL 目录
        # 却还是没被正确加进 Windows 的 DLL 搜索路径。详见
        # _ensure_sitecustomize_deployed() 的完整背景说明。
        MFAChecker._ensure_sitecustomize_deployed()

        env = dict(base_env) if base_env is not None else os.environ.copy()

        mfa_env_dir = MFAChecker.env_dir()
        env["CONDA_PREFIX"] = str(mfa_env_dir)
        mfa_lib_bin = mfa_env_dir / "Library" / "bin"
        if mfa_lib_bin.exists():
            env["PATH"] = str(mfa_lib_bin) + os.pathsep + env.get("PATH", "")
        mfa_lib_dir = mfa_env_dir / "lib"
        if mfa_lib_dir.exists():
            env["LD_LIBRARY_PATH"] = str(mfa_lib_dir) + os.pathsep + env.get("LD_LIBRARY_PATH", "")

        kaldi_env_dir = MFAChecker.kaldi_env_dir()
        if kaldi_env_dir:
            env["KALDI_ROOT"] = str(kaldi_env_dir)
            # 【修复】显式传 KALDI_ENV_DIR，而不是让子进程里的
            # mfa_env_sitecustomize.py 靠 KALDI_ROOT 兜底猜测同一个目录——
            # 两者当前设置下确实指向同一处，但语义上 KALDI_ENV_DIR 是
            # "kalpy 运行时需要 add_dll_directory() 的目录"，KALDI_ROOT 是
            # "kalpy-kaldi 内部用来定位 Kaldi 共享库的变量"，语义耦合到一起
            # 是隐患，未来两者路径分开时会悄悄失效。
            env["KALDI_ENV_DIR"] = str(kaldi_env_dir)
            kaldi_lib_bin = kaldi_env_dir / "Library" / "bin"
            if kaldi_lib_bin.exists():
                # 【说明，2026-08-16 pynini 修复】这个目录同时也是 pynini
                # 依赖的 OpenFst/OpenGrm 底层共享库（libfst*.dll、
                # libngram*.dll 等）的落地位置——因为 pynini 是和 kaldi/
                # kalpy 装进同一个 .kaldi_env 前缀的，conda 把所有原生共享
                # 库统一放在 <prefix>\Library\bin 下。不需要为 pynini 单独
                # 再加一次 PATH，这里已经覆盖到了。
                env["PATH"] = str(kaldi_lib_bin) + os.pathsep + env.get("PATH", "")
            kaldi_lib_dir = kaldi_env_dir / "lib"
            if kaldi_lib_dir.exists():
                env["LD_LIBRARY_PATH"] = str(kaldi_lib_dir) + os.pathsep + env.get("LD_LIBRARY_PATH", "")

        # 【修复，2026-08-16】不再把整个 `.kaldi_env` site-packages 塞进
        # PYTHONPATH——那样会让 `.kaldi_env` 自带的 numpy 2.x（以及 scipy/
        # librosa 等）排到 `.mfa_env` 自己 pip 装的 numpy==1.26.4 前面
        # （PYTHONPATH 目录在 sys.path 里固定排在解释器自身 site-packages
        # 之前，这个顺序 Python 启动时就定死了，没法通过调整 PYTHONPATH
        # 内部顺序绕开），导致 torch/torchaudio 这类编译时链接了 numpy
        # C-API 的包在子进程里出现 "compiled using NumPy 1.x cannot be run
        # in NumPy 2.x" ABI 崩溃，进而让整个子进程（含 sitecustomize.py 里
        # 的 speechbrain 补丁、以及紧随其后的 montreal_forced_aligner 自身
        # import）一起挂掉。改为只暴露 _kalpy_shim_dir()：一个只含
        # `_kalpy` 扩展模块本体和纯 Python `kalpy` 包的瘦身目录，不含
        # `.kaldi_env` 自带的任何第三方依赖，这样 `.mfa_env` 子进程里
        # `import numpy` 等仍然会正确解析到 `.mfa_env` 自己 site-packages
        # 里那份 numpy==1.26.4。
        kalpy_shim_dir = MFAChecker._kalpy_shim_dir()
        if kalpy_shim_dir:
            env["PYTHONPATH"] = str(kalpy_shim_dir) + os.pathsep + env.get("PYTHONPATH", "")
        else:
            # shim 搭建失败（例如 .kaldi_env 里没有正确安装 kalpy）时退回
            # 旧行为，至少保留原有能力，不会比修复前更差；check_kalpy() 里
            # 的报错信息会指出具体原因。
            kaldi_site_packages = MFAChecker.kaldi_site_packages_dir()
            if kaldi_site_packages:
                env["PYTHONPATH"] = str(kaldi_site_packages) + os.pathsep + env.get("PYTHONPATH", "")

        return env

    @staticmethod
    def env_python() -> Path:
        p = MFAChecker.env_dir() / "python.exe"
        if p.exists():
            return p
        return Path(sys.executable)

    _sitecustomize_sync_lock = threading.Lock()
    _sitecustomize_last_sync_check: float = 0.0
    _SITECUSTOMIZE_SYNC_CHECK_INTERVAL = 30.0  # 秒：避免每次子进程调用都做磁盘 IO 比较

    @staticmethod
    def _ensure_sitecustomize_deployed() -> None:
        """
        【2026-08-16 第三轮修复】

        背景（排查过程记录）：连续两次线上复现的 "_kalpy DLL load failed"，
        用手写诊断脚本（diagnose_kalpy_dll.py）在同一台机器上重建完全相同
        的 KALDI_ROOT / PYTHONPATH / add_dll_directory 之后，import _kalpy
        却能成功——这个矛盾一开始让人怀疑是并发写 .kalpy_shim 的竞态问题
        （已经修过一版加锁 + 原子改名），但加锁版本部署后问题**原样复现**，
        排除了竞态是根因。

        真正原因：diagnose_kalpy_dll.py 是在自己的代码里**手动调用了一次
        os.add_dll_directory()**，这一步绕过了 sitecustomize.py 本该做的
        事，所以诊断脚本"成功"这件事，只证明了"手动调用 add_dll_directory
        本身能让 import 成功"，并不能证明部署在 .mfa_env 里的那份
        sitecustomize.py 真的在做同样的事。

        而 setup.bat 部署 sitecustomize.py 的逻辑（Step 3.5）只在"第一次
        跑 setup.bat 完成初次安装"这个时间点执行一次
            copy backend/mfa_env_sitecustomize.py -> .mfa_env/Lib/site-packages/sitecustomize.py
        （Windows 下实际是反斜杠路径，这里为避免 Python 字符串转义解析
        写成正斜杠，含义等价）之后任何对 backend/mfa_env_sitecustomize.py
        源文件的修改（包括
        本项目历次修复：反斜杠路径判断、numpy ABI 崩溃防护等），只要没有
        重新跑一遍 setup.bat 或手动重新复制，.mfa_env 里实际生效的那份
        sitecustomize.py 就还是最早部署时的旧版本，完全不会体现任何后续
        修复——这正是"代码明明改了，现象却一模一样"的真实原因：不是修复
        无效，是修复根本没有被部署到真正生效的位置。

        本函数把"部署"这个动作从"setup.bat 里一次性的手动步骤"变成
        "每次真正需要跑 MFA 子进程之前自动核对一次源文件和已部署文件是否
        一致，不一致就自动重新复制"，从根上避免这类"改了源码但没生效"的
        情况再次发生，不再依赖开发者记得手动重新部署或重新跑 setup.bat。

        用文件内容哈希比较（而不是 mtime）判断是否需要重新部署，因为
        mtime 在 git checkout / 解压 zip / 复制粘贴等操作后不可靠（很多
        工具会保留或改写 mtime，不能保证"源文件更新 = mtime 变大"这个
        前提一定成立），内容哈希更准确地回答"这两份文件内容是否相同"这个
        真正关心的问题。
        """
        import hashlib
        import shutil

        now = time.monotonic()
        # 节流：同一进程内 30 秒之内只真正检查一次，避免每次提交对齐任务
        # 都做一次磁盘 IO + 哈希计算（sitecustomize.py 部署后基本不会在
        # 运行期间频繁变化，没必要每次都全量比较）。
        if (now - MFAChecker._sitecustomize_last_sync_check) < MFAChecker._SITECUSTOMIZE_SYNC_CHECK_INTERVAL:
            return

        with MFAChecker._sitecustomize_sync_lock:
            # 双重检查：可能在等锁的过程中，另一个线程已经做完这次检查了。
            now = time.monotonic()
            if (now - MFAChecker._sitecustomize_last_sync_check) < MFAChecker._SITECUSTOMIZE_SYNC_CHECK_INTERVAL:
                return

            try:
                src_file = MFAChecker.project_root() / "backend" / "mfa_env_sitecustomize.py"
                if not src_file.exists():
                    logger.debug("_ensure_sitecustomize_deployed: 源文件不存在，跳过: %s", src_file)
                    return

                mfa_env_dir = MFAChecker.env_dir()
                # site-packages 目录：跟 setup.bat 用同样的方式定位
                # （<mfa_env>\Lib\site-packages，Windows 下 site.getsitepackages()
                # 的第一项）。这里不方便像 setup.bat 那样起子进程去问
                # site.getsitepackages()（会有额外开销，且这个函数本身就是
                # 为了在"启动子进程之前"把事情做好），直接按 Windows 惯例
                # 路径拼，跟 kaldi_site_packages_dir() 处理 .kaldi_env 的
                # 方式保持一致的风格。
                win_site_packages = mfa_env_dir / "Lib" / "site-packages"
                if win_site_packages.exists():
                    dest_dir = win_site_packages
                else:
                    lib_dir = mfa_env_dir / "lib"
                    dest_dir = None
                    if lib_dir.exists():
                        for child in lib_dir.iterdir():
                            if child.is_dir() and child.name.startswith("python"):
                                candidate = child / "site-packages"
                                if candidate.exists():
                                    dest_dir = candidate
                                    break
                    if dest_dir is None:
                        logger.debug("_ensure_sitecustomize_deployed: 找不到 .mfa_env 的 site-packages 目录，跳过")
                        return

                dest_file = dest_dir / "sitecustomize.py"

                def _sha256(p: Path) -> Optional[str]:
                    try:
                        return hashlib.sha256(p.read_bytes()).hexdigest()
                    except OSError:
                        return None

                src_hash = _sha256(src_file)
                dest_hash = _sha256(dest_file) if dest_file.exists() else None

                if src_hash is not None and src_hash != dest_hash:
                    # 内容不一致（包括目标文件完全不存在的情况）：重新部署。
                    # 写到临时文件再原子改名，避免子进程在复制过程中途读到
                    # 半写的文件（虽然概率很低，但既然做了就顺手做对）。
                    import tempfile
                    fd, tmp_path = tempfile.mkstemp(prefix=".sitecustomize_", dir=str(dest_dir))
                    os.close(fd)
                    try:
                        shutil.copy2(str(src_file), tmp_path)
                        os.replace(tmp_path, str(dest_file))
                        logger.info(
                            "sitecustomize.py 已自动重新部署到 .mfa_env（检测到源文件与已部署版本不一致）: %s",
                            dest_file,
                        )
                    except OSError as e:
                        logger.warning("sitecustomize.py 自动重新部署失败: %s", e)
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
            finally:
                MFAChecker._sitecustomize_last_sync_check = time.monotonic()

    @staticmethod
    def mfa_root_dir() -> Path:
        """
        统一 MFA 根目录，优先读取 MFA_ROOT_DIR。
        这样可以兼容你把模型放到 E 盘的情况。
        """
        root = os.environ.get("MFA_ROOT_DIR")
        if root and Path(root).exists():
            return Path(root)
        return Path.home() / "Documents" / "MFA"

    @staticmethod
    def resolve_mfa_exe() -> Optional[Path]:
        candidates = [
            os.environ.get("MFA_EXE"),
            str(MFAChecker.env_dir() / "Scripts" / "mfa.exe"),
            str(MFAChecker.env_dir() / "Scripts" / "mfa"),
        ]
        for c in candidates:
            if c and Path(c).exists():
                return Path(c)
        return None

    @staticmethod
    def check_kalpy() -> Tuple[bool, str]:
        # 【修复 v3】kalpy 是 `conda install -c conda-forge kalpy` 装进
        # .kaldi_env 的（不是 pip、不是 .mfa_env），所以当前进程要 import
        # 它，必须先把 .kaldi_env 的 site-packages 目录塞进 sys.path——
        # 这是比 DLL 加载更前一步的问题：Python 得先知道模块在哪儿，才谈
        # 得上加载。之后仍然设置 KALDI_ROOT/PATH，让 kalpy 内部能定位到
        # Kaldi 的共享库并成功加载 DLL。
        kaldi_env_dir = MFAChecker.kaldi_env_dir()
        # 【修复，2026-08-16】同 build_kaldi_subprocess_env()：不直接把
        # `.kaldi_env` 整个 site-packages 插进 sys.path（会让它自带的
        # numpy 2.x 等排到当前进程自己的 numpy 前面，引发 ABI 冲突），
        # 改用只含 _kalpy/kalpy 本体的瘦身 shim 目录；shim 搭建失败时才
        # 退回旧行为。
        kalpy_shim_dir = MFAChecker._kalpy_shim_dir()
        kaldi_site_packages = kalpy_shim_dir or MFAChecker.kaldi_site_packages_dir()
        added_dll_dir = None
        added_sys_path = False
        if kaldi_site_packages and str(kaldi_site_packages) not in sys.path:
            sys.path.insert(0, str(kaldi_site_packages))
            added_sys_path = True
        if kaldi_env_dir:
            os.environ["KALDI_ROOT"] = str(kaldi_env_dir)
            kaldi_lib_bin = kaldi_env_dir / "Library" / "bin"
            if kaldi_lib_bin.exists():
                os.environ["PATH"] = str(kaldi_lib_bin) + os.pathsep + os.environ.get("PATH", "")
                if hasattr(os, "add_dll_directory"):
                    try:
                        added_dll_dir = os.add_dll_directory(str(kaldi_lib_bin))
                    except (OSError, FileNotFoundError):
                        pass
            kaldi_lib_dir = kaldi_env_dir / "lib"
            if kaldi_lib_dir.exists():
                os.environ["LD_LIBRARY_PATH"] = str(kaldi_lib_dir) + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")

        try:
            import _kalpy  # noqa: F401
            return True, "OK"
        except Exception as e:
            hint = ""
            if kaldi_env_dir is None:
                hint = (
                    "（未找到 kaldi 独立环境：既没有 <应用根目录>/.kaldi_env，"
                    "也没有 <应用根目录>/runtime/kaldi_env，请先运行 "
                    "setup.bat/setup.sh 创建独立 kaldi 环境，或确认打包目录"
                    "结构是否完整）"
                )
            elif kaldi_site_packages is None:
                hint = f"（{kaldi_env_dir} 里没找到 kalpy，请运行: conda install -y -p \"{kaldi_env_dir}\" -c conda-forge kalpy）"
            return False, f"{e}{hint}"
        finally:
            if added_dll_dir is not None:
                try:
                    added_dll_dir.close()
                except Exception:
                    pass

    @staticmethod
    def check_pynini() -> Tuple[bool, str]:
        """
        【新增，2026-08-16】独立检测 pynini 是否可用。

        跟 check_kalpy() 是同一套思路（同一个 .kaldi_env、同一个 shim 目录），
        但拆成单独一个函数、单独一个状态字段，而不是直接合并进
        check_kalpy() / "ready"，原因是：pynini 缺失目前只影响
        montreal_forced_aligner 内部会用到 G2P/文本规整（grapheme-to-phoneme
        规则编译）的那部分路径，不是 `mfa align` 主流程的强依赖——把它跟
        kalpy 状态混在一起会让 "kalpy 明明装好了、mfa align 能跑" 的用户
        也被挡在 ready=False 外面，诊断信息反而更不清晰。前端/日志里可以
        分别展示 kalpy_message 和 pynini_message，各自定位问题。
        """
        kaldi_env_dir = MFAChecker.kaldi_env_dir()
        kalpy_shim_dir = MFAChecker._kalpy_shim_dir()
        kaldi_site_packages = kalpy_shim_dir or MFAChecker.kaldi_site_packages_dir()
        added_dll_dir = None
        added_sys_path = False
        if kaldi_site_packages and str(kaldi_site_packages) not in sys.path:
            sys.path.insert(0, str(kaldi_site_packages))
            added_sys_path = True
        if kaldi_env_dir:
            kaldi_lib_bin = kaldi_env_dir / "Library" / "bin"
            if kaldi_lib_bin.exists() and hasattr(os, "add_dll_directory"):
                try:
                    added_dll_dir = os.add_dll_directory(str(kaldi_lib_bin))
                except (OSError, FileNotFoundError):
                    pass

        try:
            import pynini  # noqa: F401
            return True, "OK"
        except Exception as e:
            hint = ""
            if kaldi_env_dir is None:
                hint = (
                    "（未找到 kaldi 独立环境：既没有 <应用根目录>/.kaldi_env，"
                    "也没有 <应用根目录>/runtime/kaldi_env，请先运行 "
                    "setup.bat/setup.sh 创建独立 kaldi 环境，或确认打包目录"
                    "结构是否完整）"
                )
            else:
                hint = f"（{kaldi_env_dir} 里没找到 pynini，请运行: conda install -y -p \"{kaldi_env_dir}\" -c conda-forge pynini）"
            return False, f"{e}{hint}"
        finally:
            if added_dll_dir is not None:
                try:
                    added_dll_dir.close()
                except Exception:
                    pass

    @staticmethod
    def _cache_mfa_result(ok: bool, msg: str) -> None:
        """将检测结果写入 TTL 缓存（仅缓存成功结果）。"""
        if ok:
            with MFAChecker._status_cache_lock:
                MFAChecker._last_good_mfa_check = (ok, msg, time.monotonic())

    @staticmethod
    def check_mfa_installed() -> Tuple[bool, str]:
        """
        检查 MFA 是否可用，并尽量返回真实版本号。

        检测顺序（速度从快到慢）：
          1. TTL 缓存 — 120 s 内复用上一次的成功结果（0 ms）
          2. 同进程 importlib.metadata — MFA 在同一 venv 时立即返回（~1 ms）
          3. 子进程元数据查询 — MFA 在独立 venv 时调用（~5–30 s，有超时保护）
          4. 子进程 CLI 版本查询 — 最后的兜底手段

        任何一次探测成功，都缓存结果并返回实际版本字符串。
        """
        # ── 1. TTL 缓存：120 s 内直接复用成功结果 ────────────────────────────
        with MFAChecker._status_cache_lock:
            cached = MFAChecker._last_good_mfa_check
        if cached is not None:
            ok, msg, ts = cached
            if ok and (time.monotonic() - ts) < MFAChecker._MFA_CHECK_CACHE_TTL:
                logger.debug("check_mfa_installed: 命中 TTL 缓存 (%s)", msg)
                return ok, msg

        # ── 2. 同进程元数据查询（最快，无子进程开销）────────────────────────
        # Flask 与 MFA 运行在同一 venv 时，这里直接返回，整个函数开销 < 1 ms。
        # pkg_version 已在文件顶部 import，此处是第一次实际调用它。
        try:
            v = pkg_version("montreal-forced-aligner")
            if v:
                logger.info("check_mfa_installed: 同进程检测成功，版本 %s", v)
                MFAChecker._cache_mfa_result(True, v)
                return True, v
        except PackageNotFoundError:
            # MFA 不在当前 Python 环境，跌落到子进程探测
            logger.debug("check_mfa_installed: 当前进程未安装 MFA，尝试独立 venv")
        except Exception as e:
            logger.debug("check_mfa_installed: 同进程 pkg_version 异常: %s", e)

        # ── 3 & 4. 子进程探测（MFA 在独立 venv 时才走到这里）────────────────
        py = MFAChecker.env_python()
        # probe 4 会真正 import montreal_forced_aligner → _kalpy，需要
        # KALDI_ROOT/PATH 指向 .kaldi_env，否则报 ModuleNotFoundError。
        subprocess_env = MFAChecker.build_kaldi_subprocess_env()

        def _normalize_version_text(text: str) -> str:
            text = (text or "").strip()
            if not text:
                return ""
            # 取最后一行，避免前面带欢迎信息/警告
            return text.splitlines()[-1].strip()

        probes = [
            # 3) 子进程内读包元数据，比 CLI 启动更轻量
            [
                str(py),
                "-c",
                (
                    "from importlib.metadata import version; "
                    "print(version('montreal-forced-aligner'))"
                ),
            ],
            # 4) CLI 版本命令（最重，作为最终兜底）
            [str(py), "-m", "montreal_forced_aligner.command_line.mfa", "version"],
        ]

        last_msg = ""

        for cmd in probes:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=subprocess_env,
                )

                stdout = (result.stdout or "").strip()
                stderr = (result.stderr or "").strip()

                if result.returncode == 0:
                    version_text = _normalize_version_text(stdout)
                    if version_text:
                        MFAChecker._cache_mfa_result(True, version_text)
                        return True, version_text

                    if stderr:
                        # 有些环境版本号会被打到 stderr，顺手兼容一下
                        version_text = _normalize_version_text(stderr)
                        if version_text:
                            MFAChecker._cache_mfa_result(True, version_text)
                            return True, version_text

                    MFAChecker._cache_mfa_result(True, "unknown")
                    return True, "unknown"

                last_msg = stderr or stdout or f"returncode={result.returncode}"

            except subprocess.TimeoutExpired:
                last_msg = "版本检测超时"
                continue
            except Exception as e:
                last_msg = str(e)

        return False, last_msg or "MFA not detected"

    @staticmethod
    def check_model_downloaded(model_name: str, model_type: str = "acoustic") -> bool:
        """
        改成统一从 MFA_ROOT_DIR / Documents/MFA 找模型。
        """
        mfa_cache_home = MFAChecker.mfa_root_dir()

        inspect_path = mfa_cache_home / "inspect" / model_name
        if inspect_path.exists():
            logger.info(f"✓ 找到模型 {model_name} 在 inspect 路径: {inspect_path}")
            return True

        old_path = mfa_cache_home / "pretrained_models" / model_type / f"{model_name}.zip"
        if old_path.exists():
            logger.info(f"✓ 找到模型 {model_name} 在 pretrained_models 路径: {old_path}")
            return True

        dict_path = None
        if model_type == "dictionary":
            dict_path = mfa_cache_home / "pretrained_models" / model_type / f"{model_name}.dict"
            if dict_path.exists():
                logger.info(f"✓ 找到模型 {model_name} 在 pretrained_models 路径: {dict_path}")
                return True

        logger.warning(f"✗ 未找到模型 {model_name} (类型: {model_type})")
        logger.warning(f"  检查位置 1: {inspect_path}")
        logger.warning(f"  检查位置 2: {old_path}")
        if dict_path:
            logger.warning(f"  检查位置 3: {dict_path}")

        return False

    @staticmethod
    def get_status() -> Dict[str, object]:
        kalpy_ok, kalpy_msg = MFAChecker.check_kalpy()
        pynini_ok, pynini_msg = MFAChecker.check_pynini()
        mfa_ok, mfa_msg = MFAChecker.check_mfa_installed()

        models_status = {}
        if mfa_ok:
            primary_langs = ["cmn", "eng", "jpn", "kor", "yue"]
            for lang_code in primary_langs:
                models = MFAChecker.LANGUAGE_MODELS.get(lang_code)
                if not models:
                    continue

                dict_model = models["dictionary"]
                acoustic_model = models["acoustic"]

                logger.info(f"检查 {lang_code}: dictionary={dict_model}, acoustic={acoustic_model}")
                dict_ok = MFAChecker.check_model_downloaded(dict_model, "dictionary")
                acoustic_ok = MFAChecker.check_model_downloaded(acoustic_model, "acoustic")
                models_status[lang_code] = dict_ok and acoustic_ok

                logger.info(f"  {lang_code}: dict={dict_ok}, acoustic={acoustic_ok}, combined={models_status[lang_code]}")

        return {
            # 这里建议把"安装"和"可运行"分开
            "installed": bool(mfa_ok),
            "ready": bool(kalpy_ok and mfa_ok),
            "version": mfa_msg if mfa_ok else "",
            "kalpy": kalpy_ok,
            "kalpy_message": kalpy_msg,
            "pynini": pynini_ok,
            "pynini_message": pynini_msg,
            "mfa": mfa_ok,
            "mfa_message": mfa_msg,
            "mfa_version": mfa_msg if mfa_ok else "",
            "models": {
                "cmn": models_status.get("cmn", False),
                "eng": models_status.get("eng", False),
                "jpn": models_status.get("jpn", False),
                "kor": models_status.get("kor", False),
                "yue": models_status.get("yue", False),
            }
        }

    @staticmethod
    def download_model(language: str) -> Tuple[bool, str]:
        models = MFAChecker.LANGUAGE_MODELS.get(language)
        if not models:
            return False, f"Unknown language: {language}"
        
        dict_model = models["dictionary"]
        acoustic_model = models["acoustic"]
        py = MFAChecker.env_python()
        # `mfa model download` 同样会 import montreal_forced_aligner，
        # 需要同一份 KALDI_ROOT/PATH 环境，否则子进程里 import _kalpy 失败。
        subprocess_env = MFAChecker.build_kaldi_subprocess_env()
        
        results = []
        
        # 下载 Dictionary
        cmd_dict = [
            str(py),
            "-m",
            "montreal_forced_aligner.command_line.mfa",
            "model",
            "download",
            "dictionary",
            dict_model,
        ]
        try:
            result = subprocess.run(cmd_dict, capture_output=True, text=True, timeout=600, env=subprocess_env)
            if result.returncode == 0:
                results.append(f"Dictionary {dict_model} downloaded")
                # 模型下载成功后，让缓存自然过期以触发重新检测
                with MFAChecker._status_cache_lock:
                    MFAChecker._last_good_mfa_check = None
            else:
                return False, f"Dictionary download failed: {result.stderr}"
        except Exception as e:
            return False, f"Dictionary download error: {str(e)}"
        
        # 下载 Acoustic Model
        cmd_acoustic = [
            str(py),
            "-m",
            "montreal_forced_aligner.command_line.mfa",
            "model",
            "download",
            "acoustic",
            acoustic_model,
        ]
        try:
            result = subprocess.run(cmd_acoustic, capture_output=True, text=True, timeout=600, env=subprocess_env)
            if result.returncode == 0:
                results.append(f"Acoustic {acoustic_model} downloaded")
            else:
                return False, f"Acoustic download failed: {result.stderr}"
        except Exception as e:
            return False, f"Acoustic download error: {str(e)}"
        
        return True, " + ".join(results)
