@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal enabledelayedexpansion

REM ══════════════════════════════════════════════════════════════════════════
REM pack_runtime.bat — 把开发用的 conda 环境打包成可离线分发的
REM runtime\ 目录，供 launcher.py 打包出的 exe 使用。
REM
REM 背景
REM ────
REM setup.bat 在你自己机器上创建的 .mfa_env / .kaldi_env / .nemo_env（以及
REM 本脚本会补建的 .qwen3_env）是"开发环境"：路径写死在你自己电脑上，没法
REM 直接拷给用户用。launcher.py 打包发布时需要的是 runtime\mfa_env\
REM runtime\kaldi_env\ runtime\qwen3_env\ runtime\nemo_env\ 这种"可重定位"
REM 的独立 Python 运行时——用户解压后不需要装 conda/装依赖，双击 exe 就能跑。
REM
REM 其中 .kaldi_env 是 setup.bat 里新增的、单独用 conda 装 kaldi 二进制的
REM 环境（不装完整的 montreal-forced-aligner conda 包，避免带上 GDK 图形
REM 依赖），pip 版 MFA（装在 .mfa_env 里）运行时靠 PATH 找到这个环境里的
REM kaldi 可执行文件。发布时如果漏打这个环境，用户机器上跑对齐会直接报
REM "找不到 kaldi 可执行文件"，所以必须和 .mfa_env 一起打包。
REM
REM 这中间的转换工具就是 conda-pack：把一个 conda 环境连同它的全部
REM 依赖（不管是 conda 装的还是 pip 装的）打成一个 tar.gz，解压到别的
REM 机器/别的路径后，跑一下里面自带的 conda-unpack.exe 修复写死的路径，
REM 就能当一个独立、可离线运行的 Python 环境用了。
REM
REM 用法：只在"准备发布"时手动跑一次这个脚本，日常开发不需要，也不会
REM 动你的 .mfa_env / .kaldi_env / .qwen3_env / .nemo_env 本身（conda-pack
REM 只读）。跑完之后 runtime\ 目录就和 backend\ frontend\dist\ 一起，按
REM build_launcher.bat 末尾提示的方式拷进 dist\Tsubaki启动器\ 里发布。
REM
REM 注意事项（conda-pack 的已知限制，都是正常现象）：
REM   - 打包机和用户机器的操作系统必须一致（这里都是 Windows，没问题）。
REM   - 环境一旦被 conda-unpack 处理过就不能再被"重新打包/再次搬家"，
REM     所以这个脚本是直接打到最终的 runtime\<env>\ 目录，不要打包完
REM     之后再手动挪动这个文件夹。
REM   - 如果 .mfa_env 里装了 GPU 版 PyTorch（CUDA 编译版本），打出来的
REM     环境在没有对应显卡驱动的用户机器上跑不了 GPU 加速，只能退回
REM     CPU（业务逻辑本身不受影响，只是变慢）。
REM   - launcher.py 启动 MFA 相关流程前，需要把 runtime\kaldi_env\Library\bin
REM     （kaldi 可执行文件所在目录，conda-forge 的 kaldi 包在 Windows 上
REM     一般装在这里）加进子进程的 PATH，否则找不到 kaldi。具体路径解压后
REM     可以用 dir /s /b runtime\kaldi_env\*.exe | findstr /i kaldi 确认一下。
REM ══════════════════════════════════════════════════════════════════════════

REM ── 1. 找 conda（和 setup.bat 里的逻辑保持一致）──
set "CONDA_BAT="
for %%p in (
    "%USERPROFILE%\miniconda3\condabin\conda.bat"
    "%USERPROFILE%\Anaconda3\condabin\conda.bat"
    "%USERPROFILE%\miniforge3\condabin\conda.bat"
    "%ALLUSERSPROFILE%\miniconda3\condabin\conda.bat"
    "%ALLUSERSPROFILE%\Anaconda3\condabin\conda.bat"
    "%ALLUSERSPROFILE%\miniforge3\condabin\conda.bat"
) do (
    if exist "%%~p" (
        set "CONDA_BAT=%%~p"
        goto :conda_found
    )
)
for %%X in (conda.bat) do (set "CONDA_BAT=%%~$PATH:X")
:conda_found
if not defined CONDA_BAT (
    echo [x] 找不到 conda，请先安装 Miniconda3 或 Miniforge。
    pause
    exit /b 1
)
echo [√] 使用 conda: %CONDA_BAT%

REM ── 2. 确保 base 环境里有 conda-pack（只装一次，和三个业务环境无关）──
call "%CONDA_BAT%" run -n base conda pack --help >nul 2>&1
if errorlevel 1 (
    echo [*] base 环境里没有 conda-pack，正在安装...
    call "%CONDA_BAT%" install -n base -c conda-forge -y conda-pack
    if errorlevel 1 (
        echo [x] conda-pack 安装失败，请手动执行：
        echo     "%CONDA_BAT%" install -n base -c conda-forge conda-pack
        pause
        exit /b 1
    )
)

REM ── 3. qwen3_env 目前 setup.bat 还没自动创建，这里按和 nemo_env 相同的
REM      套路补一份：conda 只负责给一个干净的 Python + pip，具体依赖来自
REM      backend\requirements-qwen3.txt。已存在则直接跳过这一步。 ──
if not exist ".qwen3_env" (
    echo.
    echo [*] 未发现 .qwen3_env，正在创建（python 3.10 + requirements-qwen3.txt）...
    call "%CONDA_BAT%" create -y -p "%CD%\.qwen3_env" -c conda-forge python=3.10 pip
    if errorlevel 1 (
        echo [x] .qwen3_env 创建失败，跳过 qwen3 环境的打包。
        goto :after_qwen3_setup
    )
    call "%CONDA_BAT%" run -p "%CD%\.qwen3_env" python -m pip install --upgrade pip setuptools wheel
    call "%CONDA_BAT%" run -p "%CD%\.qwen3_env" python -m pip install -r "backend\requirements-qwen3.txt"
    if errorlevel 1 (
        echo [x] requirements-qwen3.txt 安装失败，请检查网络后重跑本脚本。
    )
)
:after_qwen3_setup

if not exist "runtime" mkdir "runtime"

REM ── 4. 依次打包各环境 ──
call :pack_one ".mfa_env"   "mfa_env"
if exist ".kaldi_env" (
    call :pack_one ".kaldi_env" "kaldi_env"
) else (
    echo.
    echo [!] 未发现 .kaldi_env——MFA 强制对齐依赖 kaldi 可执行文件，
    echo     请先跑一遍 setup.bat（会自动创建 .kaldi_env 并安装 kaldi），
    echo     再重跑本脚本，否则打包出的 runtime 里 MFA 无法正常对齐。
)
call :pack_one ".qwen3_env" "qwen3_env"
if exist ".nemo_env" (
    call :pack_one ".nemo_env" "nemo_env"
) else (
    echo.
    echo [!] 未发现 .nemo_env（NeMo Forced Aligner 是可选后端），跳过。
    echo     需要的话可以先跑 setup.bat 的第 6 步创建它，再重跑本脚本。
)

echo.
echo ══════════════════════════════════════════════════════════════════
echo  全部完成。runtime\ 目录内容：
dir /b "runtime" 2>nul
echo.
echo  接下来按 build_launcher.bat 末尾提示的方式，把
echo  backend\ frontend\dist\ runtime\ 三个目录拷进
echo  dist\Tsubaki启动器\ 里，和 exe 平级，即可整体发布。
echo  ※ launcher.py 里启动 MFA 相关子进程前，记得把
echo    runtime\kaldi_env\ 下 kaldi 可执行文件所在目录加进 PATH。
echo ══════════════════════════════════════════════════════════════════
pause
exit /b 0

REM ════════════════════════════════════════════════════════════════════
REM 子程序：pack_one <源环境相对路径> <runtime 下的目标目录名>
REM ════════════════════════════════════════════════════════════════════
:pack_one
set "SRC=%~1"
set "DST=%~2"

if not exist "%SRC%" (
    echo.
    echo [!] 未发现 %SRC%，跳过。
    goto :eof
)
if exist "runtime\%DST%" (
    echo.
    echo [!] runtime\%DST% 已存在，跳过（如需重新生成，请先手动删除
    echo     这个目录，conda-pack 处理过的环境不支持原地覆盖打包）。
    goto :eof
)

echo.
echo ──────────────────────────────────────────────────────────
echo 打包 %SRC%  →  runtime\%DST%（体积较大时可能需要几分钟）
echo ──────────────────────────────────────────────────────────
call "%CONDA_BAT%" run -n base conda pack -p "%CD%\%SRC%" -o "%TEMP%\%DST%.tar.gz" --format tar.gz
if errorlevel 1 (
    echo [x] %SRC% 打包失败，看上面的报错信息。
    goto :eof
)

mkdir "runtime\%DST%"
REM Windows 10 1803（build 17063）之后自带 bsdtar，即 System32\tar.exe，
REM 不需要额外装 7-Zip。老系统没有 tar 的话，脚本会在下面报错提示手动解压。
tar -xzf "%TEMP%\%DST%.tar.gz" -C "runtime\%DST%"
if errorlevel 1 (
    echo [x] 解压 %DST% 失败。如果你的系统没有内置 tar 命令，
    echo     请用 7-Zip 手动解压两次（先 .tar.gz 再 .tar）：
    echo       "%TEMP%\%DST%.tar.gz"  →  runtime\%DST%\
    goto :eof
)
del "%TEMP%\%DST%.tar.gz"

REM 关键一步：修复环境内部写死的旧路径（Python 可执行文件、脚本 shebang、
REM 部分包的绝对路径记录等）。必须用普通 cmd.exe 执行，不能在 Anaconda
REM Prompt / PowerShell 里做——activate.bat 依赖标准 cmd 的语法。
if exist "runtime\%DST%\Scripts\conda-unpack.exe" (
    call "runtime\%DST%\Scripts\activate.bat" && "runtime\%DST%\Scripts\conda-unpack.exe" && call "runtime\%DST%\Scripts\deactivate.bat"
    echo [√] %DST% 打包完成，路径已修复。
) else (
    echo [!] runtime\%DST% 里没找到 conda-unpack.exe——这是老版本
    echo     conda-pack 在 Windows 上偶尔出现的已知问题。请执行：
    echo       "%CONDA_BAT%" update -n base -c conda-forge conda-pack
    echo     升级后删除 runtime\%DST% 重跑本脚本。
)
goto :eof
