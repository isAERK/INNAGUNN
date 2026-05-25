@echo off
setlocal EnableExtensions
cd /d "%~dp0"

call npm install
if errorlevel 1 exit /b 1

call BUILD_SIDECAR_WINDOWS.bat
if errorlevel 1 exit /b 1

call npm run tauri:dev
pause
