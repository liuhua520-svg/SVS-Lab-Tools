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
echo               SVS Lab Aligner 完整安装程序 (Windows)
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
REM Step 2.1: 预装 PyAV 11.0.0（WhisperX 3.2.0 / faster-whisper 1.0.0）
REM -----------------------------------------------------------------
REM PyPI 上 av==11.0.0 当前会落到源码包；Windows 编译需要 FFmpeg 的 avformat.lib。
REM conda-forge 提供 Python 3.10 / win-64 的 av 11.0.0 二进制包，因此先用 conda 安装，
REM 再执行 pip requirements，pip 会直接复用已安装的 av，不再尝试源码编译。
echo [*] 安装 PyAV 11.0.0 二进制依赖（conda-forge）...
call "%CONDA_BAT%" install -y -p "%ENV_PREFIX%" -c conda-forge av=11.0.0
if errorlevel 1 (
    echo [ERROR] PyAV 11.0.0 安装失败，请检查 conda-forge 网络或上方错误。
    pause
    exit /b 1
)
echo [OK] PyAV 11.0.0 已安装
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
    call "%CONDA_BAT%" create -y -p "%KALDI_ENV_PREFIX%" -c conda-forge kaldi
    if errorlevel 1 (
        echo [ERROR] kaldi 安装失败，请检查上方报错信息（网络问题居多，可重跑本脚本）。
        pause
        exit /b 1
    )
)
echo [OK] kaldi 已安装到独立环境: %KALDI_ENV_PREFIX%
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
    echo     如果报错和 kalpy-kaldi 有关：这个包在 PyPI 上只提供源码分发，
    echo     需要本机具备 C++ 编译工具链才能装；装不上的话可以改用
    echo       "%CONDA_BAT%" install -y -p "%ENV_PREFIX%" -c conda-forge kalpy
    echo     再重跑本脚本（此时 pip 会检测到 kalpy-kaldi 已用 conda 装好而跳过）。
    pause
    exit /b 1
)
echo [OK] 所有 Python 依赖已安装
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
            call "%CONDA_BAT%" run --no-capture-output -p "%NEMO_ENV_PREFIX%" python -m pip install -r "%NEMO_REQ_FILE%"
            if errorlevel 1 (
                echo [ERROR] NeMo 依赖安装失败，可稍后手动执行：
                echo     "%CONDA_BAT%" run --no-capture-output -p "%NEMO_ENV_PREFIX%" python -m pip install -r "%NEMO_REQ_FILE%"
            ) else (
                echo [OK] NeMo Forced Aligner 依赖已安装
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
REM Step 7: Qwen3-ASR / Qwen3-ForcedAligner 独立环境（可选）
REM -----------------------------------------------------------------
cls
echo.
echo ================================================================================
echo Step 7/8: Qwen3-ASR / Qwen3-ForcedAligner 独立环境 (可选)
echo ================================================================================
echo.
echo Qwen3-ASR / Qwen3-ForcedAligner 是可选的对齐/识别后端，作为一个本地
echo 服务 (端口 5001) 供主后端通过 HTTP 调用。不安装不影响 MFA / WhisperX /
echo NeMo 后端使用。依赖见 backend\requirements-qwen3.txt (Python 3.10)。
echo.

set "QWEN3_ENV_PREFIX=%CD%\.qwen3_env"
set "QWEN3_REQ_FILE=%CD%\backend\requirements-qwen3.txt"
if /i "!INSTALL_ALL_OPTIONAL!"=="true" (
    set "QWEN3_CHOICE=y"
    echo [OK] ALL：自动安装 Qwen3-ASR/ForcedAligner，不再询问。
) else (
    set /p "QWEN3_CHOICE=是否现在创建独立环境并安装 Qwen3-ASR/ForcedAligner? (y/n): "
    if /i "!QWEN3_CHOICE!"=="all" (
        echo [OK] 已选择 ALL：后续 Qwen3-TTS 也自动安装。
        set "INSTALL_ALL_OPTIONAL=true"
        set "QWEN3_CHOICE=y"
    )
)
)

if /i "!QWEN3_CHOICE!"=="y" (
    if not exist "%QWEN3_REQ_FILE%" (
        echo [ERROR] 找不到 %QWEN3_REQ_FILE%，跳过 Qwen3-ASR 安装
    ) else (
        set "QWEN3_NEED_CREATE=1"
        set "QWEN3_CREATE_FAILED=0"

        if exist "%QWEN3_ENV_PREFIX%" (
            echo [!] Qwen3-ASR 环境已存在: %QWEN3_ENV_PREFIX%
            set /p "QWEN3_RECREATE=是否删除并重新创建? (y/n): "
            if /i "!QWEN3_RECREATE!"=="y" (
                echo 正在删除旧 Qwen3-ASR 环境...
                call "%CONDA_BAT%" env remove -y -p "%QWEN3_ENV_PREFIX%" >nul 2>&1
            ) else (
                echo [OK] 使用现有 Qwen3-ASR 环境
                set "QWEN3_NEED_CREATE=0"
            )
        )

        if "!QWEN3_NEED_CREATE!"=="1" (
            echo 创建 Qwen3-ASR 独立环境中... 请耐心等待（可能需要几分钟）...
            call "%CONDA_BAT%" create -y -p "%QWEN3_ENV_PREFIX%" -c conda-forge python=3.10 pip >nul 2>&1
            if errorlevel 1 (
                echo [ERROR] Qwen3-ASR 环境创建失败，可稍后手动执行：
                echo     "%CONDA_BAT%" create -y -p "%QWEN3_ENV_PREFIX%" -c conda-forge python=3.10 pip
                set "QWEN3_CREATE_FAILED=1"
            )
        )

        if "!QWEN3_CREATE_FAILED!"=="0" (
            echo [OK] Qwen3-ASR 环境已准备
            echo [*] 在独立环境中安装 Qwen3-ASR/ForcedAligner 依赖（安装过程会实时显示）...
            call "%CONDA_BAT%" run --no-capture-output -p "%QWEN3_ENV_PREFIX%" python -m pip install --upgrade pip setuptools wheel
            call "%CONDA_BAT%" run --no-capture-output -p "%QWEN3_ENV_PREFIX%" python -m pip install -r "%QWEN3_REQ_FILE%"
            if errorlevel 1 (
                echo [ERROR] Qwen3-ASR 依赖安装失败，可稍后手动执行：
                echo     "%CONDA_BAT%" run -p "%QWEN3_ENV_PREFIX%" python -m pip install -r "%QWEN3_REQ_FILE%"
            ) else (
                echo [OK] Qwen3-ASR/ForcedAligner 依赖已安装
                echo [OK] 首次启动 qwen3_server.py 时会自动下载模型权重
            )
        )
    )
) else (
    echo [OK] 已跳过，可后续手动运行以下命令安装：
    echo     "%CONDA_BAT%" create -y -p "%CD%\.qwen3_env" -c conda-forge python=3.10 pip
    echo     "%CONDA_BAT%" run -p "%CD%\.qwen3_env" python -m pip install -r "%QWEN3_REQ_FILE%"
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
