@echo off
chcp 65001 >nul
cd /d "%~dp0.."

git config core.hooksPath .githooks
if errorlevel 1 (
    echo 设置失败：请确认当前目录是一个 git 仓库。
    pause
    exit /b 1
)

echo.
echo 已启用：提交后自动推送到 GitHub。
echo   钩子目录：.githooks
echo   临时跳过：set SKIP_PUSH=1 后再提交
echo.
echo 取消启用：git config --unset core.hooksPath
pause
