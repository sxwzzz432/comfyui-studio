@echo off
rem ============================================================
rem  ComfyUI 出图台 · 一键启动（双击这个文件）
rem  真正的逻辑在 start.ps1，这里只负责找到 PowerShell 并调用它
rem  参数会原样透传，例如： start.bat -Port 5600 -NoBrowser
rem ============================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

set "PS_EXE="
where /q pwsh.exe
if not errorlevel 1 set "PS_EXE=pwsh.exe"
if not defined PS_EXE (
  where /q powershell.exe
  if not errorlevel 1 set "PS_EXE=powershell.exe"
)
if not defined PS_EXE (
  echo [x] 找不到 PowerShell，没法启动。
  pause
  exit /b 1
)

"%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo [x] 启动失败，退出码 %RC%
  pause
)
exit /b %RC%
