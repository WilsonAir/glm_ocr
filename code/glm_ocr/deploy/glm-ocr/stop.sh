#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${GLM_OCR_PID_FILE:-${SCRIPT_DIR}/../../logs/glm_ocr_service.pid}"

if [[ ! -f "$PID_FILE" ]]; then
  echo "GLM-OCR service is not running (PID file missing)"
  exit 0
fi

pid="$(cat "$PID_FILE")"
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  echo "Stopped GLM-OCR service: PID $pid"
else
  echo "GLM-OCR process is not running: PID $pid"
fi
rm -f "$PID_FILE"
