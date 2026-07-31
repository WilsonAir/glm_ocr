#!/usr/bin/env bash
# Start the local GLM-OCR PDF/image parse service.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${GLM_OCR_ENV_FILE:-${SCRIPT_DIR}/config.env}"

load_env() {
  [[ -f "$1" ]] || return 0
  set -a
  # shellcheck disable=SC1090
  source "$1"
  set +a
}

load_env "$ENV_FILE"

GLM_OCR_CONDA_BASE="${GLM_OCR_CONDA_BASE:-/data/wilson_2/soft/miniforge3}"
GLM_OCR_CONDA_ENV="${GLM_OCR_CONDA_ENV:-med_rag_cuda}"
GLM_OCR_PYTHON="${GLM_OCR_PYTHON:-${GLM_OCR_CONDA_BASE}/envs/${GLM_OCR_CONDA_ENV}/bin/python}"
GLM_OCR_CONFIG="${GLM_OCR_CONFIG:-${PROJECT_ROOT}/config/glm_ocr.yaml}"
GLM_OCR_HOST="${GLM_OCR_HOST:-127.0.0.1}"
GLM_OCR_PORT="${GLM_OCR_PORT:-18090}"
GLM_OCR_LAYOUT_DEVICE="${GLM_OCR_LAYOUT_DEVICE:-cuda:1}"
GLM_OCR_LOG_DIR="${GLM_OCR_LOG_DIR:-${PROJECT_ROOT}/logs}"
GLM_OCR_LOG_FILE="${GLM_OCR_LOG_FILE:-${GLM_OCR_LOG_DIR}/glm_ocr_service.log}"
GLM_OCR_PID_FILE="${GLM_OCR_PID_FILE:-${GLM_OCR_LOG_DIR}/glm_ocr_service.pid}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--foreground] [env-file]

  GLM_OCR_PYTHON          Python executable with glmocr installed
  GLM_OCR_CONFIG          SDK config YAML
  GLM_OCR_LAYOUT_DEVICE   Layout device, e.g. cuda:1 or cpu
  GLM_OCR_HOST / PORT      API bind address (default 127.0.0.1:18090)
  GLM_OCR_LOG_FILE        Service log path
  GLM_OCR_PID_FILE        PID file path
EOF
}

FOREGROUND=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --foreground|-f) FOREGROUND=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) ENV_FILE="$1"; load_env "$ENV_FILE"; shift ;;
  esac
done

[[ -x "$GLM_OCR_PYTHON" ]] || {
  echo "GLM-OCR Python not executable: $GLM_OCR_PYTHON" >&2
  exit 1
}
[[ -f "$GLM_OCR_CONFIG" ]] || {
  echo "GLM-OCR config not found: $GLM_OCR_CONFIG" >&2
  exit 1
}

mkdir -p "$GLM_OCR_LOG_DIR"
export GLM_OCR_CONFIG GLM_OCR_HOST GLM_OCR_PORT GLM_OCR_LAYOUT_DEVICE
export PYTHONUNBUFFERED=1

cmd=("$GLM_OCR_PYTHON" "${PROJECT_ROOT}/services/glm_ocr/service.py"
  --host "$GLM_OCR_HOST" --port "$GLM_OCR_PORT"
  --config "$GLM_OCR_CONFIG" --layout-device "$GLM_OCR_LAYOUT_DEVICE")

if [[ "$FOREGROUND" == true ]]; then
  exec "${cmd[@]}"
fi

if [[ -f "$GLM_OCR_PID_FILE" ]] && kill -0 "$(cat "$GLM_OCR_PID_FILE")" 2>/dev/null; then
  echo "GLM-OCR service already running: PID $(cat "$GLM_OCR_PID_FILE")" >&2
  exit 1
fi

nohup "${cmd[@]}" >>"$GLM_OCR_LOG_FILE" 2>&1 &
echo $! >"$GLM_OCR_PID_FILE"
echo "GLM-OCR service started: PID $! (${GLM_OCR_HOST}:${GLM_OCR_PORT})"
echo "Log: $GLM_OCR_LOG_FILE"
