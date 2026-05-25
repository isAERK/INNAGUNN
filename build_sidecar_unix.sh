#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

python3 -m pip install -r backend/requirements.txt pyinstaller
python3 -m PyInstaller --noconfirm --clean --onefile --console \
  --name inna_downloader_cli \
  --add-data "backend/viewer.html:." \
  --collect-all playwright \
  backend/inna_downloader.py

mkdir -p src-tauri/binaries
TARGET_TRIPLE="$(rustc --print host-tuple)"
EXT=""
if [[ "$(uname -s)" == "MINGW"* || "$(uname -s)" == "MSYS"* ]]; then
  EXT=".exe"
fi

cp "dist/inna_downloader_cli${EXT}" "src-tauri/binaries/inna_downloader_cli-${TARGET_TRIPLE}${EXT}"
echo "Built src-tauri/binaries/inna_downloader_cli-${TARGET_TRIPLE}${EXT}"
