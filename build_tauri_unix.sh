#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
npm install
./build_sidecar_unix.sh
npm run tauri:build
