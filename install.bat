@echo off
REM ============================================================
REM  Firefly 桌宠 —— 依赖安装脚本
REM  要求：Windows 10/11 + Python 3.10~3.12（64 位）
REM ============================================================
setlocal
cd /d "%~dp0"

echo ============================================
echo  Firefly Desktop Pet - Installer
echo ============================================
echo.

echo [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Python not found in PATH.
    echo Install Python 3.10-3.12 from https://www.python.org/downloads/
    echo Remember to check "Add Python to PATH".
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version') do echo    found: %%v

echo.
echo [2/4] Creating virtual environment (.venv)...
if exist ".venv\Scripts\python.exe" (
    echo    .venv already exists, skip.
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv
        pause
        exit /b 1
    )
)

echo.
echo [3/4] Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo.
echo [4/4] Installing requirements...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] pip install failed. Check your network, or use a mirror:
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Done.
echo.
echo  Next steps:
echo    1. copy  firefly_config.example.json  to  firefly_config.json
echo    2. open  firefly_config.json  and fill your API key
echo    3. double-click  start_all.vbs
echo ============================================
pause
