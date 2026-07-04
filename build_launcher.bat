@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/2] 安装打包依赖（只影响 launcher 自身，不动 mfa_env/qwen3_env/nemo_env）
python -m pip install --upgrade pyinstaller pystray pillow psutil
if errorlevel 1 (
    echo 依赖安装失败。
    pause
    exit /b 1
)

echo [2/2] 打包 launcher.py 为多文件(onedir) exe
pyinstaller ^
  --name "Tsubaki启动器" ^
  --onedir ^
  --noconsole ^
  --clean ^
  launcher.py
if errorlevel 1 (
    echo 打包失败。
    pause
    exit /b 1
)

echo.
echo 完成。产物在 dist\Tsubaki启动器\ 目录下：
echo   - Tsubaki启动器.exe
echo   - _internal\
echo.
echo 接下来把 backend\ frontend\dist\ runtime\ 这三个目录拷到
echo dist\Tsubaki启动器\ 里，和 exe 平级，即可整体发布。
pause
