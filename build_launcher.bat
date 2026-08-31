@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/2] 安装打包依赖（只影响 launcher 自身，不动 mfa_env/qwen3_env/nemo_env）
echo       pywebview 用来把界面装进原生窗口（不再弹系统浏览器）；
echo       pythonnet 是 pywebview 在 Windows 上走 EdgeChromium(WebView2) 渲染
echo       所必需的桥接库，冻结时容易漏打包，这里显式装好、下面显式 hidden-import。
python -m pip install --upgrade pyinstaller pystray pillow psutil pywebview pythonnet
if errorlevel 1 (
    echo 依赖安装失败。
    pause
    exit /b 1
)

echo [2/2] 打包 launcher.py 为多文件(onedir) exe
pyinstaller ^
  --name "SVS-Lab-Talkloid-Tools" ^
  --onedir ^
  --noconsole ^
  --clean ^
  --icon="SVS-Lab-Talkloid-Tools.ico" ^
  --hidden-import=clr_loader ^
  --hidden-import=pythonnet ^
  launcher.py
if errorlevel 1 (
    echo 打包失败。
    pause
    exit /b 1
)

echo.
echo 完成。产物在 dist\SVS-Lab-Tools\ 目录下：
echo   - SVS-Lab-Tools.exe
echo   - _internal\
echo.
echo 接下来将询问是否复制运行所需的 backend/frontend 以及虚拟环境。
echo.
echo 排错提示：
echo   - 目标机器需要 WebView2 运行时（Win10 1803+ / Win11 通常已自带；
echo     没有的话去微软官网下 "WebView2 Evergreen Bootstrapper" 装一下）。
echo   - 如果双击 exe 后窗口一闪而过或报
echo     "Failed to resolve Python.Runtime"/pythonnet 相关错误，
echo     通常是 pythonnet 版本和 clr_loader 没配对好，尝试：
echo       pip install "pythonnet>=3.0.3" "clr_loader>=0.2.6" 后重新打包。
echo.
choice /C YN /N /M "是否复制 backend/frontend 到 dist\SVS-Lab-Tools\runtime？ [Y/N]"

if errorlevel 2 (
    echo.
    echo 已选择 N，跳过 backend/frontend 复制，请手动复制。
    echo.
    pause
    exit /b 0
)

echo.
echo 已选择 Y，开始复制 backend/frontend。
echo.

echo.
echo [1/2] 复制 backend → dist\SVS-Lab-Tools\backend
robocopy ".\backend" ".\dist\SVS-Lab-Tools\backend" /E /MT:16 /R:2 /W:2

echo.
echo [2/2] 复制 .mfa_env → dist\SVS-Lab-Tools\frobtend
robocopy ".\frontend" ".\dist\SVS-Lab-Tools\frontend" /E /MT:16 /R:2 /W:2

choice /C YN /N /M "是否继续复制虚拟环境到 dist\SVS-Lab-Tools\runtime？ [Y/N]"

if errorlevel 2 (
    echo.
    echo 已选择 N，跳过虚拟环境复制，请手动复制。
    echo.
    pause
    exit /b 0
)

echo.
echo 已选择 Y，开始复制虚拟环境。
echo.

if not exist ".\dist\SVS-Lab-Tools\runtime" (
    mkdir ".\dist\SVS-Lab-Tools\runtime"
)

if not exist ".\.mfa_env" (
    echo 未检测到.mfa_env。
    pause
    exit /b 1
)

echo.
echo [1/5] 复制 .mfa_env → dist\SVS-Lab-Tools\runtime\.mfa_env
robocopy ".\.mfa_env" ".\dist\SVS-Lab-Tools\runtime\.mfa_env" /E /MT:16 /R:2 /W:2

echo.
echo [2/5] 复制 .mfa_env → dist\SVS-Lab-Tools\runtime\.kaldi_env
robocopy ".\.kaldi_env" ".\dist\SVS-Lab-Tools\runtime\.kaldi_env" /E /MT:16 /R:2 /W:2

echo.
echo [3/5] 复制 .qwen3_env → dist\SVS-Lab-Tools\runtime\.whisperx_env
robocopy ".\.qwen3_env" ".\dist\SVS-Lab-Tools\runtime\.whisperx_env" /E /MT:16 /R:2 /W:2

echo.
echo [4/5] 复制 .qwen3_env → dist\SVS-Lab-Tools\runtime\.qwen3tts_env
robocopy ".\.qwen3_env" ".\dist\SVS-Lab-Tools\runtime\.qwen3tts_env" /E /MT:16 /R:2 /W:2

echo.
echo [5/5] 复制 .nemo_env → dist\SVS-Lab-Tools\runtime\.nemo_env
robocopy ".\.nemo_env" ".\dist\SVS-Lab-Tools\runtime\.nemo_env" /E /MT:16 /R:2 /W:2

echo.
echo ============================================
echo 所有复制完成。
echo ============================================
pause