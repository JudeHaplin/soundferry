@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo 首次运行，正在创建运行环境...
    python -m venv .venv
    if errorlevel 1 (
        echo 没找到 Python，请先安装 Python 3.11 - 3.13 并勾选 Add to PATH。
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

start "" ".venv\Scripts\pythonw.exe" "app.py"
