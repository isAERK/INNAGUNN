@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo Installing frontend dependencies...
call npm install
if errorlevel 1 exit /b 1

echo Building sidecar...
call BUILD_SIDECAR_WINDOWS.bat --no-pause
if errorlevel 1 exit /b 1

echo Building Tauri app...
call npm run tauri:build -- --bundles nsis
pause
