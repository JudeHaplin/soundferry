@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   声渡 SoundFerry —— 一键同步到 GitHub
echo ============================================
echo.

git add -A
git diff --cached --quiet
if not errorlevel 1 (
    echo 没有新的改动，仓库已是最新状态。
    echo.
    git status -sb
    pause
    exit /b 0
)

echo 即将提交的改动：
git diff --cached --stat
echo.

git commit -m "chore: 同步本地改动"
if errorlevel 1 (
    echo.
    echo [失败] 提交没有完成，请检查上面的报错。
    pause
    exit /b 1
)

git push origin main
if errorlevel 1 (
    echo.
    echo [失败] 推送失败。常见原因：网络不通，或 GitHub 登录已过期。
    echo        重新登录：gh auth login
    pause
    exit /b 1
)

echo.
echo [完成] 已同步到 https://github.com/JudeHaplin/soundferry
pause
