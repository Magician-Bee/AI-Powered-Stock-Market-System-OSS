@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem Large binary wheels such as Codex CLI can exceed uv's 30-second default read timeout.
rem Preserve explicit user settings, otherwise use portable-launcher-safe values.
if not defined UV_HTTP_TIMEOUT set "UV_HTTP_TIMEOUT=300"
if not defined UV_HTTP_CONNECT_TIMEOUT set "UV_HTTP_CONNECT_TIMEOUT=30"
if not defined UV_HTTP_RETRIES set "UV_HTTP_RETRIES=8"
cd /d "%~dp0"
title Stock AI System
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-stock-ai.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo Startup failed. See the message above.
  pause
)
exit /b %EXIT_CODE%
