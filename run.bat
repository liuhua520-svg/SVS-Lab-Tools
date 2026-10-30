@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ================================================
echo    SVS Lab Tools + Qwen3-ASR + NeMo-FA + Qwen3-TTS 服务启动器
echo ================================================

set "MFA_PY=%CD%\.mfa_env\python.exe"
set "WHISPERX_PY=%CD%\.whisperx_env\python.exe"
set "NEMO_PY=%CD%\.nemo_env\python.exe"
set "TTS_PY=%CD%\.qwen3tts_env\python.exe"

if not exist "%MFA_PY%" (
    echo [错误] .mfa_env\python.exe 未找到！
    pause
    exit /b 1
)

REM 计算总步数（主后端 + 已安装的可选后端）
set /a TOTAL_STEPS=1
set "HAS_WHISPERX=0"
set "HAS_NEMO=0"
set "HAS_TTS=0"

if exist "%WHISPERX_PY%" (
    set "HAS_WHISPERX=1"
    set /a TOTAL_STEPS+=1
)
if exist "%NEMO_PY%" (
    set "HAS_NEMO=1"
    set /a TOTAL_STEPS+=1
)
if exist "%TTS_PY%" (
    set "HAS_TTS=1"
    set /a TOTAL_STEPS+=1
)

set /a STEP=1
set "WAIT_NEEDED=0"

if "%HAS_WHISPERX%"=="1" (
    echo [!STEP!/%TOTAL_STEPS%] 启动 WhisperX 推理服务（端口 5854）...
    start "WhisperX 服务" "%WHISPERX_PY%" "backend\whisperx_server.py"
    set /a STEP+=1
    set "WAIT_NEEDED=1"
) else (
    echo [警告] .whisperx_env\python.exe 未找到，将跳过 WhisperX
)

if "%HAS_NEMO%"=="1" (
    echo [!STEP!/%TOTAL_STEPS%] 启动 NeMo Forced Aligner 服务（端口 5852）...
    start "NeMo Forced Aligner 服务" "%NEMO_PY%" "backend\nemo_server.py"
    set /a STEP+=1
    set "WAIT_NEEDED=1"
) else (
    echo [警告] .nemo_env\python.exe 未找到，将跳过 NeMo Forced Aligner
)

if "%HAS_TTS%"=="1" (
    echo [!STEP!/%TOTAL_STEPS%] 启动 Qwen3-TTS 推理服务（端口 5853）...
    start "Qwen3-TTS 服务" "%TTS_PY%" "backend\qwen3tts_server.py"
    set /a STEP+=1
    set "WAIT_NEEDED=1"
) else (
    echo [警告] .qwen3tts_env\python.exe 未找到，将跳过 Qwen3-TTS
)

if "%WAIT_NEEDED%"=="1" (
    echo [等待 5 秒让后台服务完全启动...]
    timeout /t 5 /nobreak >nul
)

echo [!STEP!/%TOTAL_STEPS%] 启动主后端服务（端口 5850）...
"%MFA_PY%" backend\app.py

pause
