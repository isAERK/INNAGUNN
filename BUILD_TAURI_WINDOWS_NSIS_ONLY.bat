@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo Installing frontend dependencies...
call npm install
if errorlevel 1 exit /b 1

echo Building sidecar...
call BUILD_SIDECAR_WINDOWS.bat --no-pause
if errorlevel 1 exit /b 1

echo Building Tauri app with NSIS installer only...
call npm run tauri:build -- --bundles nsis
if errorlevel 1 exit /b 1

echo.
echo Done. Look here:
echo   src-tauri\target\release\bundle\nsis
echo.
pause
