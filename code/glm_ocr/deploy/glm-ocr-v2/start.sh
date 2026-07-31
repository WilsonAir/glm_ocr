#!/usr/bin/env bash
# Start and manage the persistent GLM-OCR v2 PDF/image parse service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
REPOSITORY_ROOT="$(cd "${PROJECT_ROOT}/../.." && pwd)"
ENV_FILE="${GLM_OCR_V2_ENV_FILE:-${SCRIPT_DIR}/config.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

GLM_OCR_V2_CONDA_BASE="${GLM_OCR_V2_CONDA_BASE:-/data/wilson_2/soft/miniforge3}"
GLM_OCR_V2_CONDA_ENV="${GLM_OCR_V2_CONDA_ENV:-med_rag_cuda}"
GLM_OCR_V2_PYTHON="${GLM_OCR_V2_PYTHON:-${GLM_OCR_V2_CONDA_BASE}/envs/${GLM_OCR_V2_CONDA_ENV}/bin/python}"
GLM_OCR_V2_CONFIG="${GLM_OCR_V2_CONFIG:-${PROJECT_ROOT}/config/glm_ocr.yaml}"
GLM_OCR_V2_HOST="${GLM_OCR_V2_HOST:-127.0.0.1}"
GLM_OCR_V2_PORT="${GLM_OCR_V2_PORT:-18091}"
GLM_OCR_V2_LAYOUT_DEVICE="${GLM_OCR_V2_LAYOUT_DEVICE:-cuda:1}"
GLM_OCR_V2_OUTPUT_ROOT="${GLM_OCR_V2_OUTPUT_ROOT:-${REPOSITORY_ROOT}/result/glm_ocr/framework}"
GLM_OCR_V2_LOG_DIR="${GLM_OCR_V2_LOG_DIR:-${PROJECT_ROOT}/logs}"
GLM_OCR_V2_LOG_FILE="${GLM_OCR_V2_LOG_FILE:-${GLM_OCR_V2_LOG_DIR}/glm_ocr_v2_service.log}"
GLM_OCR_V2_PID_FILE="${GLM_OCR_V2_PID_FILE:-${GLM_OCR_V2_LOG_DIR}/glm_ocr_v2_service.pid}"

usage() {
  echo "Usage: $(basename "$0") [--foreground|--status|--stop]"
}

running() {
  [[ -f "$GLM_OCR_V2_PID_FILE" ]] || return 1
  local pid state
  pid="$(cat "$GLM_OCR_V2_PID_FILE" 2>/dev/null)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  state="$(awk '/^State:/{print $2}' "/proc/${pid}/status" 2>/dev/null || true)"
  [[ "$state" != "Z" ]]
}

case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;
  --status)
    if running; then
      echo "GLM-OCR v2 running: PID $(cat "$GLM_OCR_V2_PID_FILE"), port=$GLM_OCR_V2_PORT"
    else
      echo "GLM-OCR v2 stopped"
    fi
    exit 0
    ;;
  --stop)
    if running; then
      kill "$(cat "$GLM_OCR_V2_PID_FILE")"
      echo "Stopped GLM-OCR v2"
    fi
    rm -f "$GLM_OCR_V2_PID_FILE"
    exit 0
    ;;
  --foreground|"")
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

[[ -x "$GLM_OCR_V2_PYTHON" ]] || {
  echo "GLM-OCR v2 Python not executable: $GLM_OCR_V2_PYTHON" >&2
  exit 1
}
[[ -f "$GLM_OCR_V2_CONFIG" ]] || {
  echo "GLM-OCR v2 config not found: $GLM_OCR_V2_CONFIG" >&2
  exit 1
}
running && {
  echo "GLM-OCR v2 already running: PID $(cat "$GLM_OCR_V2_PID_FILE")" >&2
  exit 1
}

mkdir -p "$GLM_OCR_V2_LOG_DIR" "$GLM_OCR_V2_OUTPUT_ROOT"
export PYTHONUNBUFFERED=1

cmd=(
  "$GLM_OCR_V2_PYTHON"
  "${PROJECT_ROOT}/services/glm_ocr_v2/service.py"
  --host "$GLM_OCR_V2_HOST"
  --port "$GLM_OCR_V2_PORT"
  --config "$GLM_OCR_V2_CONFIG"
  --layout-device "$GLM_OCR_V2_LAYOUT_DEVICE"
  --output-root "$GLM_OCR_V2_OUTPUT_ROOT"
)

if [[ "${1:-}" == "--foreground" ]]; then
  exec "${cmd[@]}"
fi

nohup "${cmd[@]}" >>"$GLM_OCR_V2_LOG_FILE" 2>&1 &
pid=$!
echo "$pid" >"$GLM_OCR_V2_PID_FILE"
echo "GLM-OCR v2 started: PID $pid (${GLM_OCR_V2_HOST}:${GLM_OCR_V2_PORT})"
echo "Output: $GLM_OCR_V2_OUTPUT_ROOT"
echo "Log: $GLM_OCR_V2_LOG_FILE"
