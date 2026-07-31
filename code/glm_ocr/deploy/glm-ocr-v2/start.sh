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
GLM_OCR_V2_SERVICE_CONFIG="${GLM_OCR_V2_SERVICE_CONFIG:-${PROJECT_ROOT}/config/ocr_services_v2.yaml}"
GLM_OCR_V2_CONFIG="${GLM_OCR_V2_CONFIG:-${PROJECT_ROOT}/config/glm_ocr.yaml}"
GLM_OCR_V2_HOST="${GLM_OCR_V2_HOST:-127.0.0.1}"
GLM_OCR_V2_PORT="${GLM_OCR_V2_PORT:-18091}"
GLM_OCR_V2_LAYOUT_DEVICE="${GLM_OCR_V2_LAYOUT_DEVICE:-cuda:1}"
GLM_OCR_V2_OUTPUT_ROOT="${GLM_OCR_V2_OUTPUT_ROOT:-${REPOSITORY_ROOT}/result/glm_ocr/framework}"
GLM_OCR_V2_LOG_DIR="${GLM_OCR_V2_LOG_DIR:-${PROJECT_ROOT}/logs}"
GLM_OCR_V2_LOG_FILE="${GLM_OCR_V2_LOG_FILE:-${GLM_OCR_V2_LOG_DIR}/glm_ocr_v2_service.log}"
GLM_OCR_V2_PID_FILE="${GLM_OCR_V2_PID_FILE:-${GLM_OCR_V2_LOG_DIR}/glm_ocr_v2_service.pid}"
PPU_SDK="${PPU_SDK:-/usr/local/PPU_SDK}"
PPU_HOME="${PPU_HOME:-$PPU_SDK}"
CUDA_PATH="${CUDA_PATH:-${PPU_SDK}/CUDA_SDK}"
PPU_RTC_CACHE_DIR="${PPU_RTC_CACHE_DIR:-/data/wilson_2/cache/rtccache}"
PPU_RTC_CACHE_LINK="${PPU_RTC_CACHE_LINK:-${HOME:-/home/whs}/.rtccache}"

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
[[ -f "$GLM_OCR_V2_SERVICE_CONFIG" ]] || {
  echo "GLM-OCR v2 service config not found: $GLM_OCR_V2_SERVICE_CONFIG" >&2
  exit 1
}
[[ -d "$PPU_SDK" ]] || {
  echo "PPU SDK directory not found: $PPU_SDK" >&2
  exit 1
}
[[ -d "$CUDA_PATH" ]] || {
  echo "PPU CUDA SDK directory not found: $CUDA_PATH" >&2
  exit 1
}
running && {
  echo "GLM-OCR v2 already running: PID $(cat "$GLM_OCR_V2_PID_FILE")" >&2
  exit 1
}

mkdir -p "$(dirname "$PPU_RTC_CACHE_DIR")"
if [[ -L "$PPU_RTC_CACHE_LINK" ]]; then
  current_target="$(readlink -f "$PPU_RTC_CACHE_LINK")"
  expected_target="$(readlink -f "$PPU_RTC_CACHE_DIR" 2>/dev/null || echo "$PPU_RTC_CACHE_DIR")"
  [[ "$current_target" == "$expected_target" ]] || {
    echo "PPU RTC cache link points elsewhere: $PPU_RTC_CACHE_LINK -> $current_target" >&2
    exit 1
  }
elif [[ -d "$PPU_RTC_CACHE_LINK" ]]; then
  if [[ -e "$PPU_RTC_CACHE_DIR" ]]; then
    echo "Both RTC cache paths already exist; refusing to merge automatically:" >&2
    echo "  current: $PPU_RTC_CACHE_LINK" >&2
    echo "  target:  $PPU_RTC_CACHE_DIR" >&2
    exit 1
  fi
  mv "$PPU_RTC_CACHE_LINK" "$PPU_RTC_CACHE_DIR"
  ln -s "$PPU_RTC_CACHE_DIR" "$PPU_RTC_CACHE_LINK"
  echo "Moved PPU RTC cache to $PPU_RTC_CACHE_DIR"
elif [[ -e "$PPU_RTC_CACHE_LINK" ]]; then
  echo "PPU RTC cache path is not a directory: $PPU_RTC_CACHE_LINK" >&2
  exit 1
else
  mkdir -p "$PPU_RTC_CACHE_DIR"
  mkdir -p "$(dirname "$PPU_RTC_CACHE_LINK")"
  ln -s "$PPU_RTC_CACHE_DIR" "$PPU_RTC_CACHE_LINK"
fi

mkdir -p "$GLM_OCR_V2_LOG_DIR" "$GLM_OCR_V2_OUTPUT_ROOT"
export PPU_SDK PPU_HOME CUDA_PATH
export PATH="${CUDA_PATH}/bin:${PATH}"
export LD_LIBRARY_PATH="${PPU_SDK}/lib:${CUDA_PATH}/lib64:${LD_LIBRARY_PATH:-}"
export PYTHONUNBUFFERED=1

cmd=(
  "$GLM_OCR_V2_PYTHON"
  "${PROJECT_ROOT}/services/glm_ocr_v2/service.py"
  --service-config "$GLM_OCR_V2_SERVICE_CONFIG"
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
