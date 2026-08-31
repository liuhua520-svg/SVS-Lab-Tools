@echo off
REM SVS Lab Aligner 完整一键安装脚本 (Windows)
chcp 936 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "ENV_PREFIX=%CD%\.mfa_env"
set "KALDI_ENV_PREFIX=%CD%\.kaldi_env"
set "REQUIREMENTS_FILE=%CD%\backend\requirements.txt"

REM 定义MFA支持的语言
set "LANGUAGES=cmn eng jpn kor yue"
set "LANG_NAME_cmn=中文普通话"
set "LANG_NAME_eng=英语"
set "LANG_NAME_jpn=日语"
set "LANG_NAME_kor=韩语"
set "LANG_NAME_yue=粤语"

cls
echo.
echo ================================================================================
echo.
echo               SVS Lab Tools 完整安装程序 (Windows)
echo.
echo   本脚本将自动完成以下步骤:
echo     - 检查 Conda 和 Node.js 环境
echo     - 创建虚拟环境并根据 requirements.txt 安装所有依赖
echo     - 安装并构建 Vue 前端
echo     - 交互式选择语言模型并下载
echo     - (可选) 创建独立环境并安装 NeMo Forced Aligner
echo     - (可选) 创建独立环境并安装 Qwen3-ASR/ForcedAligner
echo     - (可选) 创建独立环境并安装 Qwen3-TTS
echo.
echo   预计耗时: 15-30 分钟 (取决于网络和模型大小)
echo.
echo ================================================================================
echo.
pause

REM -----------------------------------------------------------------
REM Step 1: 检查系统依赖 (Conda & Node)
REM -----------------------------------------------------------------
cls
echo.
echo ================================================================================
echo Step 1/8: 检查系统依赖
echo ================================================================================
echo.

REM 1.1 动态寻找 Conda
set "CONDA_BAT="
for %%p in (
    "%USERPROFILE%\miniconda3\condabin\conda.bat"
    "%USERPROFILE%\Anaconda3\condabin\conda.bat"
	"%USERPROFILE%\miniforge3\condabin\conda.bat"
    "%ALLUSERSPROFILE%\miniconda3\condabin\conda.bat"
    "%ALLUSERSPROFILE%\Anaconda3\condabin\conda.bat"
	"%ALLUSERSPROFILE%\miniforge3\condabin\conda.bat"
    "C:\ProgramData\Miniconda3\condabin\conda.bat"
    "C:\ProgramData\Anaconda3\condabin\conda.bat"
	"C:\ProgramData\Miniforge3\condabin\conda.bat"
	
) do (
    if exist "%%~p" (
        set "CONDA_BAT=%%~p"
        goto :conda_found
    )
)
REM 检查环境变量 PATH 中是否有 conda
for %%X in (conda.bat) do (set "CONDA_BAT=%%~$PATH:X")

:conda_found
if not defined CONDA_BAT (
    echo [ERROR] Conda 未找到！请确保已安装 Miniconda3 或 Miniforge。
    echo   Miniconda3 下载地址: https://docs.conda.io/projects/miniconda/en/latest/
	echo   Miniforge 下载地址: https://github.com/conda-forge/miniforge/releases
    pause
    exit /b 1
)
echo [OK] 发现 Conda: %CONDA_BAT%

REM 1.2 检查 Node.js
where node >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js 未找到！前端构建需要 Node.js 环境。
    echo   下载地址: https://nodejs.org/
    pause
    exit /b 1
)
echo [OK] 发现 Node.js

REM -----------------------------------------------------------------
REM Step 2: 创建 Conda 环境
REM -----------------------------------------------------------------
cls
echo.
echo ================================================================================
echo Step 2/8: 创建 MFA 虚拟环境
echo ================================================================================
echo.
echo 环境位置: %ENV_PREFIX%
echo.

if exist "%ENV_PREFIX%" (
    echo [!] 发现已存在的环境
    set /p "CHOICE=是否删除并重新创建? (y/n): "
    if /i "!CHOICE!"=="y" (
        echo 正在删除旧环境...
        call "%CONDA_BAT%" env remove -y -p "%ENV_PREFIX%" >nul 2>&1
    ) else (
        echo [OK] 使用现有环境
        goto :skip_env_create
    )
)

echo 创建环境中... 请耐心等待（可能需要几分钟）...
call "%CONDA_BAT%" create -y -p "%ENV_PREFIX%" -c conda-forge python=3.10 pip >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 环境创建失败
    pause
    exit /b 1
)

:skip_env_create
echo [OK] MFA 环境已准备
echo.

REM -----------------------------------------------------------------
REM Step 2.5: 用 conda 在【独立环境】里只装 kaldi 这个二进制依赖
REM -----------------------------------------------------------------
REM 【修复】此前这一步是 `conda install -p "%ENV_PREFIX%" ... kaldi`，把
REM kaldi 直接装进了 .mfa_env 内部，与 setup.sh（Linux/Mac 版）创建独立
REM .kaldi_env 的做法不一致，也与 pack_runtime.bat / mfa_utils.py 里
REM kaldi_env_dir() 的假设（kaldi 应该在独立的 .kaldi_env 目录下）对不上。
REM 装进同一个环境虽然靠 mfa_utils.py 的兜底逻辑仍能跑起来，但会导致：
REM   1) pack_runtime.bat 检测不到 .kaldi_env，打出来的 runtime\ 缺少
REM      kaldi_env 这个独立目录，只是"凑巧"因为 kaldi 已经在 mfa_env 里
REM      而不出错，属于貌合神离；
REM   2) 以后想换掉/升级 kaldi 版本时，不能只重建 .kaldi_env，必须连
REM      .mfa_env 一起重建，麻烦得多。
REM 现在改为在独立的 .kaldi_env 里装 kaldi，与 setup.sh 保持一致。
REM
REM 注意：这里故意不用 `conda install -c conda-forge montreal-forced-aligner`
REM （官方 conda 完整包），因为那个包在 Windows 上会连带装上一堆 GDK/pango/
REM cairo 之类的图形渲染依赖（通常是给 fstdraw 之类的可视化子命令用的），
REM 且经常在这一步因为 post-link 脚本（gdk-pixbuf-query-loaders 等）报错
REM 装不上。MFA 官方文档也说明支持"conda 只装 kaldi/pynini 这些二进制，
REM MFA 本体走 pip"的混合安装方式，二者效果一致，且不会碰 GDK 组件。
REM MFA 核心的强制对齐流程只依赖 kaldi 可执行文件本身，装不装 GDK 完全
REM 不影响对齐结果，只是不能用可视化调试命令而已。
echo [*] 创建独立的 kaldi 环境 (.kaldi_env)...
if exist "%KALDI_ENV_PREFIX%" (
    echo [OK] .kaldi_env 已存在，跳过创建
) else (
    REM 【修复】必须显式指定 python=3.10，和 .mfa_env 保持一致！否则 conda
    REM 会按当前 conda-forge 上最新的 Python 版本解析依赖（截至这次修复时
    REM 实测会装出 Python 3.14），导致装进来的 kalpy 编译出的 _kalpy.pyd
    REM 是 cp314 ABI 的二进制扩展模块，而 .mfa_env 跑的是 Python 3.10——
    REM 两者 ABI 不兼容，3.10 解释器找不到/加载不了 cp314 的 .pyd，表现为
    REM `import _kalpy` 报 ModuleNotFoundError（哪怕文件真实存在于
    REM site-packages 里，PYTHONPATH 也指对了，Python 的 import 机制在扫描
    REM 扩展模块时也会因为 ABI tag 不匹配而当作"没有这个模块"跳过，不会
    REM 报出更明确的 ABI 不匹配错误，非常容易误判成"没装/没找到"）。
    call "%CONDA_BAT%" create -y -p "%KALDI_ENV_PREFIX%" -c conda-forge python=3.10 kaldi
    if errorlevel 1 (
        echo [ERROR] kaldi 安装失败，请检查上方报错信息（网络问题居多，可重跑本脚本）。
        pause
        exit /b 1
    )
)
echo [OK] kaldi 已安装到独立环境: %KALDI_ENV_PREFIX%
echo.

REM -----------------------------------------------------------------
REM Step 2.6: 在【同一个】 .kaldi_env 里装 kalpy（MFA 的 Kaldi Python 绑定）
REM -----------------------------------------------------------------
REM 【修复】kaldi 和 kalpy 是 conda-forge 上两个独立的包：kaldi 只提供裸的
REM Kaldi 可执行文件/DLL 本身，kalpy 才是真正给 montreal-forced-aligner
REM 调用的 pybind11 绑定，提供 _kalpy / kalpy 这两个 Python 模块。此前这
REM 一步只装了 kaldi，没装 kalpy，导致 mfa align / mfa version 等命令
REM 一律报 "ModuleNotFoundError: No module named '_kalpy'"。
REM 装进 .kaldi_env（而不是 .mfa_env），是因为：
REM   1) kalpy 依赖 kaldi 的共享库，装在同一个 conda 前缀里最省心，conda
REM      会自动处理好两者的版本匹配；
REM   2) .mfa_env 走的是 pip 安装 montreal-forced-aligner（见 Step 3），
REM      刻意不通过 conda 装完整包以避免 GDK/pango 图形依赖，kalpy 单独
REM      装进 .kaldi_env 不会触发这些依赖；
REM   3) mfa_utils.py 的 build_kaldi_subprocess_env() 会在启动 mfa 子进程
REM      时把 .kaldi_env 的 site-packages 目录通过 PYTHONPATH 传给
REM      .mfa_env 的 Python，让它能找到装在 .kaldi_env 里的 kalpy。
REM 【关键】上面创建 .kaldi_env 时必须已经指定了 python=3.10——kalpy 会
REM 编译出一个和 .kaldi_env 的 Python 版本绑定的二进制扩展模块
REM （_kalpy.cp3XX-win_amd64.pyd），如果 .kaldi_env 和 .mfa_env 的 Python
REM 大版本不一致（例如 .kaldi_env 意外装成了 3.14 而 .mfa_env 是 3.10），
REM 即使这里 kalpy 装成功、PYTHONPATH 也传对了，.mfa_env 的 Python 依然
REM 无法加载这个 ABI 不匹配的 .pyd，还是会报 ModuleNotFoundError。
echo [*] 在 .kaldi_env 中安装 kalpy（MFA 的 Kaldi Python 绑定）...
call "%CONDA_BAT%" install -y -p "%KALDI_ENV_PREFIX%" -c conda-forge kalpy
if errorlevel 1 (
    echo [ERROR] kalpy 安装失败，请检查上方报错信息（网络问题居多，可重跑本脚本）。
    echo     也可稍后手动执行：
    echo       "%CONDA_BAT%" install -y -p "%KALDI_ENV_PREFIX%" -c conda-forge kalpy
    pause
    exit /b 1
)
echo [OK] kalpy 已安装到独立环境: %KALDI_ENV_PREFIX%
echo.

REM 【安全检查】如果 .kaldi_env 是旧版脚本（未锁定 python 版本）创建后
REM 残留下来的，即使这里 kalpy 装"成功"了，ABI 也可能和 .mfa_env 对不上。
REM 直接核对 _kalpy 编译出的 .pyd 文件名里的 cp3XX 标签是否为 cp310，
REM 尽早暴露问题，而不是等用户实际跑对齐任务时才看到一头雾水的
REM ModuleNotFoundError。
set "KALPY_PYD_FOUND="
for %%F in ("%KALDI_ENV_PREFIX%\Lib\site-packages\_kalpy.cp310-win_amd64.pyd") do (
    if exist "%%F" set "KALPY_PYD_FOUND=1"
)
if not defined KALPY_PYD_FOUND (
    echo [ERROR] 未找到 _kalpy.cp310-win_amd64.pyd —— .kaldi_env 里 kalpy
    echo     编译出的扩展模块可能不是 Python 3.10 版本，和 .mfa_env 的
    echo     Python 3.10 ABI 不兼容，运行时会报 ModuleNotFoundError。
    echo     这通常是 .kaldi_env 由旧版脚本（未锁定 python 版本）创建后
    echo     残留下来的。请手动删除后重跑本脚本：
    echo       rmdir /s /q "%KALDI_ENV_PREFIX%"
    pause
    exit /b 1
)
echo [OK] kalpy 的 _kalpy.cp310-win_amd64.pyd 版本与 .mfa_env 匹配
echo.
REM -----------------------------------------------------------------
REM Step 2.7: 在同一个 .kaldi_env 里安装 pynini（MFA 的 G2P/文本规整依赖）
REM -----------------------------------------------------------------
REM 【修复】montreal_forced_aligner\data.py 在 import 时会无条件
REM `import pynini`，pynini 是 OpenFst/OpenGrm 的 C++ 绑定，和 kaldi/kalpy
REM 一样只能从 conda-forge 装，PyPI 上只有 manylinux 的 wheel，Windows
REM 下 pip 根本装不出来。之前本脚本只特化处理了 kaldi/kalpy，漏掉了
REM pynini，表现为运行时报 "ModuleNotFoundError: No module named
REM 'pynini'"。装进同一个 .kaldi_env（而不是另开一个环境）是因为：
REM   1) 和 kalpy 一样需要链接同一套 OpenFst 共享库，装在同一个 conda
REM      前缀里 conda 会自动处理好二者的版本匹配；
REM   2) backend\mfa_utils.py 的 _kalpy_shim_dir() 已经同步扩展为会把
REM      pynini/pywrapfst 也一并收进那个只含必要文件的瘦身 shim 目录，
REM      不需要再为 pynini 单独做一套 PYTHONPATH 注入逻辑。
REM 【非致命】pynini 目前只在部分语言的 G2P/文本规整路径上用到，
REM 不是 mfa align 主流程的强依赖，所以这里安装失败时不中断安装，只给出
REM 警告，避免因为这个可选依赖的网络波动阻塞整个安装流程。
echo [*] 在 .kaldi_env 中安装 pynini（MFA 的 G2P/文本规整依赖）...
call "%CONDA_BAT%" install -y -p "%KALDI_ENV_PREFIX%" -c conda-forge pynini
if errorlevel 1 (
    echo [!] pynini 安装失败（非致命，不中断安装）。部分语言的文本规整/G2P
    echo     功能可能会报 ModuleNotFoundError: No module named 'pynini'。
    echo     可稍后手动重试：
    echo       "%CONDA_BAT%" install -y -p "%KALDI_ENV_PREFIX%" -c conda-forge pynini
) else (
    echo [OK] pynini 已安装到独立环境: %KALDI_ENV_PREFIX%
)
echo.

REM -----------------------------------------------------------------
REM Step 3: 安装依赖 (通过 requirements.txt)
REM -----------------------------------------------------------------
cls
echo.
echo ================================================================================
echo Step 3/8: 安装 Python 依赖
echo ================================================================================
echo.

if not exist "%REQUIREMENTS_FILE%" (
    echo [ERROR] 找不到 %REQUIREMENTS_FILE%
    pause
    exit /b 1
)

echo [*] 升级 pip/setuptools/wheel...
call "%CONDA_BAT%" run --no-capture-output -p "%ENV_PREFIX%" python -m pip install --upgrade pip setuptools wheel

echo [*] 根据 requirements.txt 安装所有依赖 (含 pip 版 montreal-forced-aligner)...
echo   requirements.txt 里的 montreal-forced-aligner 这里走 pip 安装（纯 Python
echo   胶水代码，运行时调用上面独立 .kaldi_env 里装好的 kaldi 可执行文件），
echo   而不是走 conda 的完整包，因此不会再触发 GDK 组件安装。
echo   请耐心等待，这可能需要较长时间...
call "%CONDA_BAT%" run --no-capture-output -p "%ENV_PREFIX%" python -m pip install -r "%REQUIREMENTS_FILE%"
if errorlevel 1 (
    echo [ERROR] 依赖安装失败，请检查上方报错信息。
    pause
    exit /b 1
)
echo [OK] 所有 Python 依赖已安装
echo.

REM -----------------------------------------------------------------
REM Step 3.5: 部署 speechbrain Windows 路径分隔符 bug 补丁
REM -----------------------------------------------------------------
REM 【修复】speechbrain（MFA 通过 alt_aligners.py 用到的可选依赖）在
REM Windows 上有一个懒加载模块判断路径分隔符时写死了正斜杠的 bug，会导致
REM MFA 跑 MFCC 特征提取时，一次纯粹的内部 inspect.stack() 探测被错误地
REM 升级成真实的 import 尝试，进而因为 k2/flair 等未安装的可选依赖直接
REM 报错中断对齐任务（表现为 "ModuleNotFoundError: No module named 'k2'"
REM 或类似信息，MFA 自己的报错文案会提示"改用 Python 3.12"或"装
REM standard-aifc standard-sunau"，这两条都是误导，跟真正原因无关，照做
REM 也解决不了问题）。
REM 详见 backend\mfa_env_sitecustomize.py 文件头部注释。
REM
REM sitecustomize.py 是 CPython 标准钩子，只要放在 site-packages 根目录下
REM 就会在该环境每次启动时自动生效，不需要 MFA 或任何调用方代码显式
REM import 它，因此可以在这里"部署一次，之后 MFA 每次跑对齐都自动生效"。
REM 【修复】之前直接用 for /f 捕获 `conda run ... python -c "..."` 的 stdout
REM 来拿 site-packages 路径，实测不稳定：conda run 的输出在不同 conda
REM 版本上时不时会混进额外的提示行/空行（社区已有多个上游 issue
REM 专门讨论 conda run 输出捕获不准确的问题），`for /f` 只会保留最后
REM 一行非空输出并覆盖变量，如果最后一行恰好不是真正的路径（比如
REM 是个空行或 conda 自己的提示），MFA_SITE_PACKAGES 会被设成错误的值，而
REM `if not defined MFA_SITE_PACKAGES` 这个检查只能检查变量是否为空，检查不出
REM "值是错的这种情况"，导致后面 copy 静静失败或复制到错误位置、且不会报错。
REM 加一道最低限度的合法性检查（路径必须存在且是目录），避免即使文件里写进去的
REM 是一个看起来像路径但实际不存在的字符串、后面 copy 时一头雾水地失败。
REM 进一步加固：不用内联 `python -c "..."`，改为先写一个临时 .py 脚本文件在
REM 磁盘上。`python -c` 这一层内联字符串要经过 batch -> conda.bat -> cmd /c 多层
REM 转发，嵌套引号/转义在某些 conda 版本上存在被错误拆分的风险（尤其是项目路径
REM 包含空格时）；写成独立的 .py 文件后只需要传一个文件路径给 python，彻底避开多层
REM 引号转义问题。
REM
REM 【关键修复，实测确认】最初用 `site.getsitepackages()[0]` 取第一个候选路径，
REM 结果在这台机器的 conda/Windows 组合下，`getsitepackages()[0]` 返回的是
REM 环境根目录本身（例如 F:\svslabtools\.mfa_env），而不是期望的
REM F:\svslabtools\.mfa_env\Lib\site-packages —— 这个路径本身是真实存在的目录
REM （环境根目录当然存在），所以脚本里"路径必须存在"这道合法性检查完全没能
REM 拦下这个错误，copy 命令"成功"把 sitecustomize.py 复制到了环境根目录，而不是
REM site-packages 目录，导致 Python 启动时根本不会 import 到它，补丁没有真正生效，
REM 且没有任何报错或警告能提示这一点。
REM `site.getsitepackages()` 在不同平台/发行版上返回的候选列表顺序和内容并不
REM 保证一致（POSIX 下通常是 <prefix>/lib/pythonX.Y/site-packages，但 Windows 下
REM 有的构建会把 <prefix> 本身也列进候选列表，且不保证在哪个位置），不能依赖
REM "第一项就是 site-packages" 这个假设。改用 `sysconfig.get_paths()["purelib"]`
REM ——这是标准库里专门用来精确回答"这个 Python 解释器的纯 Python 第三方包应该
REM 装在哪"这个问题的 API，跟 pip 实际安装目标目录用的是同一套逻辑，不存在
REM "返回的是环境根目录还是 site-packages 子目录"这种歧义。
set "MFA_SITE_PACKAGES_TMP=%TEMP%\mfa_site_packages_%RANDOM%.txt"
set "MFA_SITE_PACKAGES_SCRIPT=%TEMP%\mfa_site_packages_%RANDOM%.py"
(
    echo import sysconfig
    echo with open^(r"%MFA_SITE_PACKAGES_TMP%", "w", encoding="utf-8"^) as _f:
    echo     _f.write^(sysconfig.get_paths^(^)["purelib"]^)
) > "%MFA_SITE_PACKAGES_SCRIPT%"
call "%CONDA_BAT%" run --no-capture-output -p "%ENV_PREFIX%" python "%MFA_SITE_PACKAGES_SCRIPT%"
del /q "%MFA_SITE_PACKAGES_SCRIPT%" >nul 2>&1
set "MFA_SITE_PACKAGES="
if exist "%MFA_SITE_PACKAGES_TMP%" (
    set /p "MFA_SITE_PACKAGES=" < "%MFA_SITE_PACKAGES_TMP%"
    del /q "%MFA_SITE_PACKAGES_TMP%" >nul 2>&1
)
REM 【关键修复，实测确认】仅仅"路径存在"不足以说明这是正确的 site-packages
REM 目录——之前 sysconfig.get_paths()["purelib"] 换掉 site.getsitepackages()[0]
REM 之前，拿到的错误路径（环境根目录）同样是真实存在的目录，"存在性检查"
REM 完全没能拦下这个问题。这里再加一道最低成本的额外校验：路径的最后一段
REM 目录名必须是 site-packages（大小写不敏感，Windows 文件系统本身也不区分），
REM 不满足就当作获取失败处理，避免同类"看似合法但语义不对"的路径蒙混过关。
for %%D in ("%MFA_SITE_PACKAGES%") do set "MFA_SITE_PACKAGES_LEAF=%%~nxD"
if not defined MFA_SITE_PACKAGES (
    echo [!] 未能定位 .mfa_env 的 site-packages 目录，跳过 speechbrain 补丁部署
    echo     （不影响大部分功能，只有在 MFA 报 k2/flair 相关 ImportError 时才需要它）
) else if not exist "%MFA_SITE_PACKAGES%" (
    echo [!] 获取到的 .mfa_env site-packages 路径不存在，跳过 speechbrain 补丁部署：%MFA_SITE_PACKAGES%
    echo     （conda run 输出可能被其他信息污染，可手动确认路径后重试）
) else if /i not "%MFA_SITE_PACKAGES_LEAF%"=="site-packages" (
    echo [!] 获取到的路径末级目录名不是 site-packages，跳过 speechbrain 补丁部署，避免装错位置：%MFA_SITE_PACKAGES%
    echo     （可能是 sysconfig.get_paths 在这套 Python/conda 组合下返回了非预期的路径，可手动确认后重试）
) else (
    if exist "%CD%\backend\mfa_env_sitecustomize.py" (
        copy /y "%CD%\backend\mfa_env_sitecustomize.py" "%MFA_SITE_PACKAGES%\sitecustomize.py" >nul
        if errorlevel 1 (
            echo [!] speechbrain 补丁部署失败（非致命，跳过）
        ) else if not exist "%MFA_SITE_PACKAGES%\sitecustomize.py" (
            REM 【关键修复，实测确认】copy 命令的 errorlevel 是 0（"成功"）不代表
            REM 目标文件真的落盘——之前调试时曾出现 copy 报告成功、但由于目标目录
            REM 判断错误导致文件实际没有出现在预期位置的情况。这里加一道二次验证，
            REM 不满足就明确报错而不是盲目打印 [OK]。
            echo [!] copy 命令返回成功但目标文件未出现在磁盘上，请手动确认: %MFA_SITE_PACKAGES%\sitecustomize.py
        ) else (
            echo [OK] speechbrain Windows 路径分隔符补丁已部署到 .mfa_env\Lib\site-packages\sitecustomize.py: %MFA_SITE_PACKAGES%
        )
    ) else (
        echo [!] 未找到 backend\mfa_env_sitecustomize.py，跳过补丁部署
    )
)
echo.

REM -----------------------------------------------------------------
REM Step 4: 安装并构建前端
REM -----------------------------------------------------------------
cls
echo.
echo ================================================================================
echo Step 4/8: 安装并构建前端
echo ================================================================================
echo.

cd frontend
if not exist "package.json" (
    echo [ERROR] 未找到 frontend\package.json
    cd ..
    pause
    exit /b 1
)

echo [*] 安装 npm 包...
call npm install --legacy-peer-deps >nul 2>&1
if errorlevel 1 (
    echo [ERROR] npm 依赖安装失败
    cd ..
    pause
    exit /b 1
)
echo [OK] npm 依赖已安装

echo [*] 构建前端应用...
call npm run build >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 前端构建失败
    cd ..
    pause
    exit /b 1
)
echo [OK] 前端已构建
cd ..

REM -----------------------------------------------------------------
REM Step 5: 语言模型配置
REM -----------------------------------------------------------------
cls
echo.
echo ================================================================================
echo Step 5/8: 下载 MFA 语言模型
echo ================================================================================
echo.
echo 支持的语言:
echo   - cmn - %LANG_NAME_cmn%
echo   - eng - %LANG_NAME_eng%
echo   - jpn - %LANG_NAME_jpn%
echo   - kor - %LANG_NAME_kor%
echo   - yue - %LANG_NAME_yue%
echo.
echo 说明:
echo   输入 y     - 下载该语言的预训练模型
echo   输入 n     - 跳过该语言
echo   输入 all   - 下载所有剩余语言
echo.

set "INSTALL_ALL=false"
set "SELECTED_LANGS="

for %%L in (%LANGUAGES%) do (
    if "!INSTALL_ALL!"=="true" (
        set "CHOICE=y"
    ) else (
        set /p "CHOICE=下载 %%L - !LANG_NAME_%%L! (y/n/all): "
    )
    
    if /i "!CHOICE!"=="all" (
        echo [OK] 选择所有剩余语言
        set "INSTALL_ALL=true"
        set "SELECTED_LANGS=!SELECTED_LANGS! %%L"
    ) else if /i "!CHOICE!"=="y" (
        echo [OK] 选择 %%L
        set "SELECTED_LANGS=!SELECTED_LANGS! %%L"
    ) else (
        echo [!] 跳过 %%L
    )
)

echo.
if "!SELECTED_LANGS!"=="" (
    echo [!] 未选择任何语言模型，可后续手动下载。
) else (
    echo [OK] 开始下载选定模型: !SELECTED_LANGS!
    echo.
    for %%L in (!SELECTED_LANGS!) do (
        echo [*] 下载 %%L 模型...
        call "%CONDA_BAT%" run -p "%ENV_PREFIX%" python -c "import sys; sys.path.insert(0, 'backend'); from mfa_utils import MFAChecker; success, msg = MFAChecker.download_model('%%L'); sys.exit(0 if success else 1)"
        if errorlevel 1 (
            echo   [!] %%L 模型下载失败，请检查网络后重试。
        ) else (
            echo   [OK] %%L 模型已下载
        )
    )
)

REM -----------------------------------------------------------------
REM Step 6: NeMo Forced Aligner 独立环境（可选）
REM -----------------------------------------------------------------
cls
echo.
echo ================================================================================
echo Step 6/8: NeMo Forced Aligner 独立环境 (可选)
echo ================================================================================
echo.
echo NeMo Forced Aligner 是一个可选的对齐后端 (NVIDIA CTC 强制对齐)。
echo 由于 nemo_toolkit 对 packaging/fsspec/omegaconf/hydra-core/lightning
echo 等核心依赖有严格版本限制，与主环境一起安装会产生依赖冲突，
echo 因此它需要运行在独立的环境里，作为一个本地服务 (端口 5002)
echo 供主后端通过 HTTP 调用。不安装也完全不影响 MFA / WhisperX / Qwen3 后端使用。
echo.

set "NEMO_ENV_PREFIX=%CD%\.nemo_env"
set "NEMO_REQ_FILE=%CD%\backend\requirements-nemo.txt"
set "INSTALL_ALL_OPTIONAL=false"
set /p "NEMO_CHOICE=安装可选独立环境? (y/n/all): "

if /i "!NEMO_CHOICE!"=="all" (
    echo [OK] 已选择 ALL：自动安装 NeMo + Qwen3-ASR/ForcedAligner + Qwen3-TTS，不再重复询问。
    set "INSTALL_ALL_OPTIONAL=true"
    set "NEMO_CHOICE=y"
)

if /i "!NEMO_CHOICE!"=="y" (

    REM 【2026-08 新增】让用户先选择硬件版本（CUDA 12.1 / CUDA 11.8 / CPU），
    REM 避免像之前那样在 requirements-nemo.txt 里靠注释切换、注释和实际生效
    REM 的行对不上。选择结果决定第 2 步安装 torch/torchaudio 时用哪个包名
    REM 和哪个 --index-url。
    echo.
    echo 请选择 NeMo 使用的 PyTorch 版本：
    echo   [1] CUDA 12.1（RTX 30/40/50 系等现代独立显卡）
    echo   [2] CUDA 11.8（GTX10 系等旧款显卡）
    echo   [3] CPU Only（无独立显卡）
    set "NEMO_HW_CHOICE="
    set /p "NEMO_HW_CHOICE=请输入数字 (1/2/3): "

    if "!NEMO_HW_CHOICE!"=="1" (
        set "NEMO_TORCH_PKGS=torch==2.6.0+cu121 torchaudio==2.6.0+cu121"
        set "NEMO_TORCH_INDEX=https://download.pytorch.org/whl/cu121"
        set "NEMO_HW_LABEL=CUDA 12.1"
    ) else if "!NEMO_HW_CHOICE!"=="2" (
        set "NEMO_TORCH_PKGS=torch==2.6.0+cu118 torchaudio==2.6.0+cu118"
        set "NEMO_TORCH_INDEX=https://download.pytorch.org/whl/cu118"
        set "NEMO_HW_LABEL=CUDA 11.8"
    ) else if "!NEMO_HW_CHOICE!"=="3" (
        set "NEMO_TORCH_PKGS=torch==2.6.0+cpu torchaudio==2.6.0+cpu"
        set "NEMO_TORCH_INDEX=https://download.pytorch.org/whl/cpu"
        set "NEMO_HW_LABEL=CPU Only"
    ) else (
        echo [警告] 未识别的输入 "!NEMO_HW_CHOICE!"，默认使用 CPU Only
        set "NEMO_TORCH_PKGS=torch==2.6.0+cpu torchaudio==2.6.0+cpu"
        set "NEMO_TORCH_INDEX=https://download.pytorch.org/whl/cpu"
        set "NEMO_HW_LABEL=CPU Only（默认）"
    )
    echo [OK] 已选择: !NEMO_HW_LABEL!
    echo.

    set "NEMO_NEED_CREATE=1"
    set "NEMO_CREATE_FAILED=0"

    if exist "%NEMO_ENV_PREFIX%" (
        echo [!] NeMo 环境已存在: %NEMO_ENV_PREFIX%
        set /p "NEMO_RECREATE=是否删除并重新创建? (y/n): "
        if /i "!NEMO_RECREATE!"=="y" (
            echo 正在删除旧 NeMo 环境...
            call "%CONDA_BAT%" env remove -y -p "%NEMO_ENV_PREFIX%" >nul 2>&1
        ) else (
            echo [OK] 使用现有 NeMo 环境
            set "NEMO_NEED_CREATE=0"
        )
    )

    if "!NEMO_NEED_CREATE!"=="1" (
        echo 创建 NeMo 独立环境中... 请耐心等待（可能需要几分钟）...
        call "%CONDA_BAT%" create -y -p "%NEMO_ENV_PREFIX%" -c conda-forge python=3.10 pip >nul 2>&1
        if errorlevel 1 (
            echo [ERROR] NeMo 环境创建失败，可稍后手动执行：
            echo     "%CONDA_BAT%" create -y -p "%NEMO_ENV_PREFIX%" -c conda-forge python=3.10 pip
            set "NEMO_CREATE_FAILED=1"
        )
    )

    if "!NEMO_CREATE_FAILED!"=="0" (
        echo [OK] NeMo 环境已准备
        if not exist "%NEMO_REQ_FILE%" (
            echo [ERROR] 找不到 %NEMO_REQ_FILE%
        ) else (
            echo [*] 根据 requirements-nemo.txt 安装 NeMo 独立环境依赖（安装过程会实时显示）...
            call "%CONDA_BAT%" run --no-capture-output -p "%NEMO_ENV_PREFIX%" python -m pip install --upgrade pip setuptools wheel

            REM 【2026-08 修复】分两步安装，避免 torch 那行的 --index-url
            REM 污染 flask/nemo_toolkit 等包的下载源（--index-url 会整体替换默认
            REM PyPI 源，一旦被 pip 解析到，前面所有包都会去 PyTorch 专属源里
            REM 找，结果全部找不到、整个安装中止，flask 也不会被装上）。
            echo [*] 第 1/2 步：安装 flask / nemo_toolkit / soundfile 等基础依赖...
            call "%CONDA_BAT%" run --no-capture-output -p "%NEMO_ENV_PREFIX%" python -m pip install flask==2.3.3 "nemo_toolkit[asr]==2.7.3" soundfile==0.12.1 requests tqdm
            set "NEMO_STEP1_FAILED=!errorlevel!"

            echo [*] 第 2/2 步：安装 torch / torchaudio（!NEMO_HW_LABEL!）...
            call "%CONDA_BAT%" run --no-capture-output -p "%NEMO_ENV_PREFIX%" python -m pip install !NEMO_TORCH_PKGS! --index-url !NEMO_TORCH_INDEX!
            set "NEMO_STEP2_FAILED=!errorlevel!"

            if not "!NEMO_STEP1_FAILED!"=="0" (
                echo [ERROR] 第 1 步（flask/nemo_toolkit 等）安装失败，可稍后手动执行：
                echo     "%CONDA_BAT%" run --no-capture-output -p "%NEMO_ENV_PREFIX%" python -m pip install flask==2.3.3 "nemo_toolkit[asr]==2.7.3" soundfile==0.12.1 requests tqdm
            )
            if not "!NEMO_STEP2_FAILED!"=="0" (
                echo [ERROR] 第 2 步（torch/torchaudio，!NEMO_HW_LABEL!）安装失败，可稍后手动执行：
                echo     "%CONDA_BAT%" run --no-capture-output -p "%NEMO_ENV_PREFIX%" python -m pip install !NEMO_TORCH_PKGS! --index-url !NEMO_TORCH_INDEX!
            )
            if "!NEMO_STEP1_FAILED!"=="0" if "!NEMO_STEP2_FAILED!"=="0" (
                echo [OK] NeMo Forced Aligner 依赖已安装（!NEMO_HW_LABEL!）
                echo [OK] 首次启动 nemo_server.py 时会按所选语言自动下载模型权重（数百 MB ~ 1GB）
            )
        )
    )
) else (
    echo [OK] 已跳过，可后续手动运行以下命令安装：
    echo     "%CONDA_BAT%" create -y -p "%CD%\.nemo_env" -c conda-forge python=3.10 pip
    echo     "%CONDA_BAT%" run --no-capture-output -p "%CD%\.nemo_env" python -m pip install -r "%NEMO_REQ_FILE%"
)

REM -----------------------------------------------------------------
REM Step 7: WhisperX 独立环境（可选）
REM -----------------------------------------------------------------
cls
echo.
echo ================================================================================
echo Step 7/8: WhisperX 独立环境 (可选)
echo ================================================================================
echo.
echo WhisperX 是可选的强制对齐后端，作为一个本地服务 (端口 5854) 供主后端
echo 通过 HTTP 调用。不安装不影响 MFA / Qwen3-ASR / Qwen3-FA / NeMo 后端
echo 使用。依赖见 backend\requirements-whisperx.txt (Python 3.10)。
echo.

set "WHISPERX_ENV_PREFIX=%CD%\.whisperx_env"
set "WHISPERX_REQ_FILE=%CD%\backend\requirements-whisperx.txt"
if /i "!INSTALL_ALL_OPTIONAL!"=="true" (
    set "WHISPERX_CHOICE=y"
    echo [OK] ALL：自动安装 WhisperX，不再询问。
) else (
    set /p "WHISPERX_CHOICE=是否现在创建独立环境并安装 WhisperX? (y/n): "
    if /i "!WHISPERX_CHOICE!"=="all" (
        echo [OK] 已选择 ALL：后续 Qwen3-TTS 也自动安装。
        set "INSTALL_ALL_OPTIONAL=true"
        set "WHISPERX_CHOICE=y"
    )
)
)

if /i "!WHISPERX_CHOICE!"=="y" (
    if not exist "%WHISPERX_REQ_FILE%" (
        echo [ERROR] 找不到 %WHISPERX_REQ_FILE%，跳过 WhisperX 安装
    ) else (
        set "WHISPERX_NEED_CREATE=1"
        set "WHISPERX_CREATE_FAILED=0"

        if exist "%WHISPERX_ENV_PREFIX%" (
            echo [!] WhisperX 环境已存在: %WHISPERX_ENV_PREFIX%
            set /p "WHISPERX_RECREATE=是否删除并重新创建? (y/n): "
            if /i "!WHISPERX_RECREATE!"=="y" (
                echo 正在删除旧 WhisperX 环境...
                call "%CONDA_BAT%" env remove -y -p "%WHISPERX_ENV_PREFIX%" >nul 2>&1
            ) else (
                echo [OK] 使用现有 WhisperX 环境
                set "WHISPERX_NEED_CREATE=0"
            )
        )

        if "!WHISPERX_NEED_CREATE!"=="1" (
            echo 创建 WhisperX 独立环境中... 请耐心等待（可能需要几分钟）...
            call "%CONDA_BAT%" create -y -p "%WHISPERX_ENV_PREFIX%" -c conda-forge python=3.10 pip >nul 2>&1
            if errorlevel 1 (
                echo [ERROR] WhisperX 环境创建失败，可稍后手动执行：
                echo     "%CONDA_BAT%" create -y -p "%WHISPERX_ENV_PREFIX%" -c conda-forge python=3.10 pip
                set "WHISPERX_CREATE_FAILED=1"
            )
        )

        if "!WHISPERX_CREATE_FAILED!"=="0" (
            REM 预装 PyAV 11.0.0（WhisperX 3.2.0 / faster-whisper 1.0.0 依赖）：
            REM PyPI 上 av==11.0.0 当前会落到源码包；Windows 编译需要 FFmpeg 的
            REM avformat.lib。conda-forge 提供 Python 3.10 / win-64 的 av 11.0.0
            REM 二进制包，因此先用 conda 装，再执行 pip requirements，pip 会
            REM 直接复用已安装的 av，不再尝试源码编译。这一步现在跟着 WhisperX
            REM 一起搬到了 .whisperx_env 里（此前误装在 .mfa_env，但 PyAV 只有
            REM whisperx/faster-whisper 需要，主环境不再需要它）。
            echo [*] 安装 PyAV 11.0.0 二进制依赖（conda-forge）...
            call "%CONDA_BAT%" install -y -p "%WHISPERX_ENV_PREFIX%" -c conda-forge av=11.0.0
            if errorlevel 1 (
                echo [ERROR] PyAV 11.0.0 安装失败，请检查 conda-forge 网络或上方错误，可稍后手动执行：
                echo     "%CONDA_BAT%" install -y -p "%WHISPERX_ENV_PREFIX%" -c conda-forge av=11.0.0
                set "WHISPERX_CREATE_FAILED=1"
            ) else (
                echo [OK] PyAV 11.0.0 已安装
            )
        )

        if "!WHISPERX_CREATE_FAILED!"=="0" (
            echo [OK] WhisperX 环境已准备
            echo [*] 在独立环境中安装 WhisperX 依赖（安装过程会实时显示）...
            call "%CONDA_BAT%" run --no-capture-output -p "%WHISPERX_ENV_PREFIX%" python -m pip install --upgrade pip setuptools wheel
            call "%CONDA_BAT%" run --no-capture-output -p "%WHISPERX_ENV_PREFIX%" python -m pip install -r "%WHISPERX_REQ_FILE%"
            if errorlevel 1 (
                echo [ERROR] WhisperX 依赖安装失败，可稍后手动执行：
                echo     "%CONDA_BAT%" run -p "%WHISPERX_ENV_PREFIX%" python -m pip install -r "%WHISPERX_REQ_FILE%"
            ) else (
                echo [OK] WhisperX 依赖已安装
                echo [OK] 首次启动 whisperx_server.py 时会自动下载模型权重
            )
        )
    )
) else (
    echo [OK] 已跳过，可后续手动运行以下命令安装：
    echo     "%CONDA_BAT%" create -y -p "%CD%\.whisperx_env" -c conda-forge python=3.10 pip
    echo     "%CONDA_BAT%" install -y -p "%CD%\.whisperx_env" -c conda-forge av=11.0.0
    echo     "%CONDA_BAT%" run -p "%CD%\.whisperx_env" python -m pip install -r "%WHISPERX_REQ_FILE%"
)

REM -----------------------------------------------------------------
REM Step 8: Qwen3-TTS 独立环境（可选）
REM -----------------------------------------------------------------
cls
echo.
echo ================================================================================
echo Step 8/8: Qwen3-TTS 独立环境 (可选)
echo ================================================================================
echo.
echo Qwen3-TTS 是可选的语音合成后端 (Custom Voice / Voice Design / Voice
echo Clone^)，作为一个本地服务 ^(端口 5003^) 供主后端通过 HTTP 调用。不安装
echo 不影响其它功能使用。官方要求 Python 3.12，依赖见
echo backend\requirements-qwen3tts.txt。
echo.

set "QWEN3TTS_ENV_PREFIX=%CD%\.qwen3tts_env"
set "QWEN3TTS_REQ_FILE=%CD%\backend\requirements-qwen3tts.txt"
if /i "!INSTALL_ALL_OPTIONAL!"=="true" (
    set "QWEN3TTS_CHOICE=y"
    echo [OK] ALL：自动安装 Qwen3-TTS，不再询问。
) else (
    set /p "QWEN3TTS_CHOICE=是否现在创建独立环境并安装 Qwen3-TTS? (y/n): "
    if /i "!QWEN3TTS_CHOICE!"=="all" (
        echo [OK] 已选择 ALL，安装 Qwen3-TTS。
        set "INSTALL_ALL_OPTIONAL=true"
        set "QWEN3TTS_CHOICE=y"
    )
)
)

if /i "!QWEN3TTS_CHOICE!"=="y" (
    if not exist "%QWEN3TTS_REQ_FILE%" (
        echo [ERROR] 找不到 %QWEN3TTS_REQ_FILE%，跳过 Qwen3-TTS 安装
    ) else (
        set "QWEN3TTS_NEED_CREATE=1"
        set "QWEN3TTS_CREATE_FAILED=0"

        if exist "%QWEN3TTS_ENV_PREFIX%" (
            echo [!] Qwen3-TTS 环境已存在: %QWEN3TTS_ENV_PREFIX%
            set /p "QWEN3TTS_RECREATE=是否删除并重新创建? (y/n): "
            if /i "!QWEN3TTS_RECREATE!"=="y" (
                echo 正在删除旧 Qwen3-TTS 环境...
                call "%CONDA_BAT%" env remove -y -p "%QWEN3TTS_ENV_PREFIX%" >nul 2>&1
            ) else (
                echo [OK] 使用现有 Qwen3-TTS 环境
                set "QWEN3TTS_NEED_CREATE=0"
            )
        )

        if "!QWEN3TTS_NEED_CREATE!"=="1" (
            echo 创建 Qwen3-TTS 独立环境中... 请耐心等待（可能需要几分钟）...
            call "%CONDA_BAT%" create -y -p "%QWEN3TTS_ENV_PREFIX%" -c conda-forge python=3.12 pip >nul 2>&1
            if errorlevel 1 (
                echo [ERROR] Qwen3-TTS 环境创建失败，可稍后手动执行：
                echo     "%CONDA_BAT%" create -y -p "%QWEN3TTS_ENV_PREFIX%" -c conda-forge python=3.12 pip
                set "QWEN3TTS_CREATE_FAILED=1"
            )
        )

        if "!QWEN3TTS_CREATE_FAILED!"=="0" (
            echo [OK] Qwen3-TTS 环境已准备
            echo [*] 在独立环境中安装 Qwen3-TTS 依赖（安装过程会实时显示）...
            call "%CONDA_BAT%" run --no-capture-output -p "%QWEN3TTS_ENV_PREFIX%" python -m pip install --upgrade pip setuptools wheel
            call "%CONDA_BAT%" run --no-capture-output -p "%QWEN3TTS_ENV_PREFIX%" python -m pip install -r "%QWEN3TTS_REQ_FILE%"
            if errorlevel 1 (
                echo [ERROR] Qwen3-TTS 依赖安装失败，可稍后手动执行：
                echo     "%CONDA_BAT%" run -p "%QWEN3TTS_ENV_PREFIX%" python -m pip install -r "%QWEN3TTS_REQ_FILE%"
            ) else (
                echo [OK] Qwen3-TTS 依赖已安装
                echo [OK] 首次启动 qwen3tts_server.py 时会自动下载模型权重
            )
        )
    )
) else (
    echo [OK] 已跳过，可后续手动运行以下命令安装：
    echo     "%CONDA_BAT%" create -y -p "%CD%\.qwen3tts_env" -c conda-forge python=3.12 pip
    echo     "%CONDA_BAT%" run -p "%CD%\.qwen3tts_env" python -m pip install -r "%QWEN3TTS_REQ_FILE%"
)

REM -----------------------------------------------------------------
REM 结束
REM -----------------------------------------------------------------
cls
echo.
echo ================================================================================
echo                        安装全部完成
echo ================================================================================
echo.
echo 下一步:
echo    双击运行 run.bat 即可启动应用程序。
echo.
pause
exit /b 0
