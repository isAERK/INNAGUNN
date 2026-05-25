@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo Installing Python backend requirements...
py -m pip install -r backend\requirements.txt pyinstaller
if errorlevel 1 exit /b 1

echo Building Python downloader sidecar...
if exist build rmdir /S /Q build
if not exist src-tauri\binaries mkdir src-tauri\binaries

py -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "inna_downloader_cli" ^
  --add-data "backend\viewer.html;." ^
  --collect-all playwright ^
  backend\inna_downloader.py
if errorlevel 1 exit /b 1

for /f "delims=" %%T in ('rustc --print host-tuple') do set TARGET_TRIPLE=%%T
if "%TARGET_TRIPLE%"=="" set TARGET_TRIPLE=x86_64-pc-windows-msvc

copy /Y "dist\inna_downloader_cli.exe" "src-tauri\binaries\inna_downloader_cli-%TARGET_TRIPLE%.exe"
echo Built src-tauri\binaries\inna_downloader_cli-%TARGET_TRIPLE%.exe
if /I "%~1"=="--no-pause" exit /b 0
pause
