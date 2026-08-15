# -*- coding: utf-8 -*-
"""
mfa_env_sitecustomize.py
─────────────────────────────────────────────────────────────────────────────
部署方式：本文件内容需要被复制/安装为 .mfa_env 内部的
    .mfa_env\\Lib\\site-packages\\sitecustomize.py   (Windows)
    .mfa_env/lib/pythonX.Y/site-packages/sitecustomize.py   (Linux/Mac)
sitecustomize.py 是 CPython 的标准钩子：只要它能被 import 到（即在
site-packages 根目录下），每次该环境的 python.exe 启动时都会被自动
执行，无需 MFA 或任何调用方代码显式 import 它。这是修复本文件所述问题
的唯一可行位置——本项目的 MFA 对齐是通过
    .mfa_env\\python.exe -m montreal_forced_aligner.command_line.mfa ...
以全新子进程方式调用的（见 mfa_processor.py / mfa_utils.py），跟运行
主 Flask 服务的那个 Python 进程完全隔离，主进程里 alt_aligners.py 打的
补丁（transformers / torch.load 那些）不会传递到这个子进程里。

═════════════════════════════════════════════════════════════════════════════
背景：speechbrain LazyModule 在 Windows 上的路径分隔符 bug
═════════════════════════════════════════════════════════════════════════════
现象：
    MFA 跑 MFCC 特征提取时，内部调用 librosa.load(...)，librosa 用
    lazy_loader 库做包内懒加载，lazy_loader.load() 内部会调用
    inspect.stack() 检查调用者所在模块。inspect.stack() 会遍历
    sys.modules 里的*所有*模块，对每一个执行 hasattr(module, "__file__")
    之类的探测。

    speechbrain（MFA 的可选依赖之一，这里因为 alt_aligners.py 需要用它
    加载 Qwen3-ForcedAligner 才装的）在自己包顶层把一些可选子包
    （integrations.k2_fsa、integrations.nlp 等，分别对应 k2-fsa、flair
    等第三方库）注册成了"懒加载占位对象"塞进 sys.modules——这类占位对象
    只要被 hasattr() 碰一下任意属性，就会触发它真正尝试 import 对应的
    可选依赖；如果那个可选依赖没装（本项目没装 k2，也用不到 k2 相关
    功能），就会在这次纯粹为了内部记账用途的探测里直接抛出 ImportError，
    并把这个探测过程中的失败错误地包装成"librosa/MFA 本身坏了"的样子
    往上抛，跟真正原因（k2 没装、且这次探测本来就不该触发真正的 import）
    完全对不上。

    表现为形如
        ModuleNotFoundError: No module named 'k2'
        ImportError: Lazy import of LazyModule(...integrations.k2_fsa...) failed
    的报错，最终被 MFA 自己的异常处理包装成更加无关的提示：
        "If you're running on Python 3.13, please reinstall the environment
         with Python 3.12 ... or run `pip install standard-aifc standard-sunau`"
    这条提示只是 MFA 对"MFCC 计算过程中出现任意 ImportError"的统一兜底
    文案，本项目用的是 Python 3.10，跟这条提示描述的场景无关，不用
    真的去装 standard-aifc/standard-sunau 或换 Python 版本。

根本原因（已核实，speechbrain 自己也预料到了这种情况，但判断逻辑在
Windows 上失效）：
    speechbrain/utils/importutils.py 的 LazyModule.ensure_module() 里
    本来就有一道专门防这种"被 inspect 意外摸到而触发的导入"的判断：

        importer_frame = inspect.getframeinfo(sys._getframe(stacklevel + 1))
        if importer_frame is not None and importer_frame.filename.endswith("/inspect.py"):
            raise AttributeError()   # 静默放弃，不真正触发 import

    但 `.endswith("/inspect.py")` 这个判断硬编码了 Unix 风格的正斜杠。
    Windows 上 inspect.py 的真实路径形如
        F:\\SVSLABTOOLS\\.mfa_env\\lib\\inspect.py
    用的是反斜杠，`.endswith("/inspect.py")` 恒为 False，这道防护形同
    虚设——同一次探测在 Linux/Mac 上会被正确挡掉（大概率是这个 bug 在
    speechbrain 上游至今没人报的原因），在 Windows 上就会真的尝试 import
    那个可选子模块，装没装全看运气。

═════════════════════════════════════════════════════════════════════════════
这里的做法
═════════════════════════════════════════════════════════════════════════════
不去装 k2（体积大、只能从 k2-fsa 官方 Hugging Face 仓库下载跟 torch
具体版本强绑定的 wheel、且本项目实际不使用 k2 相关功能，为了绕开一次
误触发的探测而引入这样一个重量级又脆弱的依赖不划算），而是在 Python
解释器启动时就把 speechbrain 那道防护逻辑本身修好：把路径分隔符判断
换成 os.path 风格（同时兼容正斜杠/反斜杠），使其在 Windows 上也能正确
识别"这次调用来自 inspect.py 内部，应该静默放弃"，从而让 librosa 那次
纯粹的内部探测重新表现为原本设计的行为：不触发真正的 import，也就不会
因为 k2/flair 等任何未安装的可选依赖而抛错。

优点：
  - 只修正一个被官方自己认定为"意外触发不应导入"的判断条件，不改变任何
    正常、有意为之的 import 行为。
  - 不依赖修改 speechbrain 包本身的文件（pip 升级/重装 speechbrain
    不会丢失这个修复，因为修复在 sitecustomize.py 里，是运行时打的
    monkeypatch，与 site-packages 里 speechbrain 的实际文件内容无关）。
  - 对速度、内存、依赖体积没有任何额外开销。
  - 如果 speechbrain 上游后续修好了这个 bug，这里的 monkeypatch 只是
    把同一个方法换成等价实现，不会产生冲突或双重修复的问题。
"""
import os
import sys


def _add_kaldi_dll_directory_for_kalpy() -> None:
    """
    修复：`mfa align` 等真正的对齐子进程报
        ImportError: DLL load failed while importing _kalpy: 找不到指定的模块。
    而不是 ModuleNotFoundError。

    根本原因：
    build_kaldi_subprocess_env()（mfa_utils.py）已经正确地把 .kaldi_env 的
    site-packages 塞进了 PYTHONPATH（让 Python 找得到 _kalpy.pyd 这个文件
    本身），也把 .kaldi_env\\Library\\bin 加进了 PATH 环境变量——但 Python
    3.8+ 在 Windows 上出于安全考虑，加载扩展模块（.pyd）的底层 DLL 依赖时
    **不再**默认搜索 PATH，必须显式调用 os.add_dll_directory() 才行（这也是
    mfa_utils.py 的 check_kalpy() 里已经在做的事——但那只覆盖了"同进程检测"
    这条路径，覆盖不到真正跑 `mfa align` 的独立子进程）。

    这里作为 sitecustomize.py 的一部分，会在 .mfa_env\\python.exe 启动时
    （也就是真正跑 MFA 对齐的那个子进程里）自动执行，是唯一能在子进程内
    补上 add_dll_directory() 这一步的位置：环境变量可以通过 subprocess 的
    env 参数传给子进程，但 add_dll_directory() 是进程内 API，没有对应的
    环境变量等价物，必须在子进程自己的 Python 解释器里调用一次。
    """
    if not hasattr(os, "add_dll_directory"):
        # 非 Windows（Linux/Mac 走 LD_LIBRARY_PATH，PATH 搜索规则不受影响）
        return

    kaldi_env_dir = os.environ.get("KALDI_ENV_DIR")
    if not kaldi_env_dir:
        # 兜底：build_kaldi_subprocess_env() 会设置 KALDI_ROOT，如果
        # KALDI_ENV_DIR 没传过来，退而用 KALDI_ROOT（两者在当前设置下指向
        # 同一个 .kaldi_env 目录）。
        kaldi_env_dir = os.environ.get("KALDI_ROOT")

    if not kaldi_env_dir:
        return

    kaldi_lib_bin = os.path.join(kaldi_env_dir, "Library", "bin")
    if os.path.isdir(kaldi_lib_bin):
        try:
            os.add_dll_directory(kaldi_lib_bin)
        except (OSError, FileNotFoundError):
            pass


def _patch_speechbrain_lazy_module_windows_path_bug() -> None:
    """
    【2026-08-16 补充加固】

    这个函数本身只是一个"锦上添花"的修复（见文件头注释），不是 MFA 正常
    运行的硬性前提——没打上这个补丁，MFA 大多数场景仍能正常工作，只有
    在触发 speechbrain 那个 Windows 路径分隔符 bug 的具体场景下才会报错。
    但 `from speechbrain.utils import importutils` 这一行会执行
    speechbrain 包的 `__init__.py`，而 speechbrain 核心链路会 import
    torchaudio——如果当前解释器能找到的 numpy 和编译 torch/torchaudio 时
    用的 numpy 大版本不一致（例如被 PYTHONPATH 意外插入了另一个环境自带
    的 numpy，历史上发生过一次，见 mfa_utils.py 的 _kalpy_shim_dir()
    注释），torchaudio 在 import 阶段会直接抛出 ABI 不兼容错误。

    这类错误此前观察到并不总是普通的 `ImportError`/`ModuleNotFoundError`
    ——numpy 的 C-API ABI 检查失败在某些 numpy/torch 版本组合下会以更底层
    的方式（例如 C 扩展初始化失败转译成 `SystemError`，或是在极端情况下
    直接让解释器崩出一条未被 Python 异常体系正常捕获的 fatal error）呈现，
    单纯 `except Exception` 不一定能兜住所有变体。这里保留原有的
    `except Exception`，但把它做成两层：外层再加一道更宽的兜底，确保
    "speechbrain 补丁打不上"这件本身不影响主功能的小事，绝不会连累
    `.mfa_env\\python.exe` 整个解释器启动失败——解释器启动失败的后果比
    "补丁没打上"严重得多，会连累后面完全无关的 `import
    montreal_forced_aligner` / `import _kalpy` 一起失败（这正是之前
    误以为 `_kalpy` DLL 问题复发的原因：其实是本函数更早的一步就先把
    进程崩了，`_kalpy` 那部分代码根本没机会执行到）。
    """
    try:
        _patch_speechbrain_lazy_module_windows_path_bug_impl()
    except BaseException as e:
        # 【关键】故意用 BaseException 而不是 Exception：这道防线的唯一
        # 目的是保证"打补丁"这一步无论以任何方式失败（包括理论上不太
        # 应该出现、但实际观察到会绕过普通 except Exception 的底层 ABI/
        # 初始化错误），都不能让 sitecustomize.py 的执行中断、进而让整个
        # 解释器启动失败。只吞掉异常并打印一行诊断信息到 stderr，不
        # re-raise，不影响后续代码（包括 _add_kaldi_dll_directory_for_kalpy
        # 已经跑过、以及后面 montreal_forced_aligner 自己的 import）正常
        # 继续执行。
        try:
            print(
                f"[sitecustomize] speechbrain Windows 路径修复补丁跳过"
                f"（不影响核心功能）: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
        except Exception:
            pass


def _patch_speechbrain_lazy_module_windows_path_bug_impl() -> None:
    from speechbrain.utils import importutils as _sb_importutils

    _LazyModule = getattr(_sb_importutils, "LazyModule", None)
    if _LazyModule is None:
        return

    _original_ensure_module = _LazyModule.ensure_module
    if getattr(_original_ensure_module, "_tsubaki_patched", False):
        return  # 已经打过补丁（例如被重复 import），避免重复包裹

    import inspect

    def _is_inspect_py(filename: str) -> bool:
        # 【修复点】原版只判断 filename.endswith("/inspect.py")，Windows 上
        # inspect.py 的真实路径用反斜杠分隔（如
        # F:\SVSLABTOOLS\.mfa_env\lib\inspect.py），这道判断恒为 False。
        #
        # 这里不能直接用 os.path.basename()：它按"当前运行时所在操作系统"
        # 的规则切分路径，在 Windows 上跑没问题，但如果有人在非 Windows
        # 环境（比如本补丁的单元测试、或者未来这个项目跑在 WSL/Linux 下）
        # 里传入一个带反斜杠的 Windows 风格路径字符串，os.path.basename
        # 在 POSIX 系统上并不认反斜杠是分隔符，同样会切不出 "inspect.py"。
        # 所以改成显式同时按正斜杠和反斜杠切分，不依赖当前运行平台，两种
        # 路径风格都能正确识别；再做大小写不敏感比较以兼容大小写不敏感的
        # 文件系统（如 Windows 默认的 NTFS）。
        tail = filename.replace("\\", "/").rsplit("/", 1)[-1]
        return tail.lower() == "inspect.py"

    def _patched_ensure_module(self, stacklevel: int):
        importer_frame = None
        try:
            importer_frame = inspect.getframeinfo(sys._getframe(stacklevel + 1))
        except AttributeError:
            pass

        if importer_frame is not None and _is_inspect_py(importer_frame.filename):
            raise AttributeError()

        if self.lazy_module is None:
            import importlib
            try:
                if self.package is None:
                    self.lazy_module = importlib.import_module(self.target)
                else:
                    self.lazy_module = importlib.import_module(
                        f".{self.target}", self.package
                    )
            except Exception as e:
                raise ImportError(f"Lazy import of {self!r} failed") from e

        return self.lazy_module

    _patched_ensure_module._tsubaki_patched = True
    _LazyModule.ensure_module = _patched_ensure_module


_add_kaldi_dll_directory_for_kalpy()
_patch_speechbrain_lazy_module_windows_path_bug()
