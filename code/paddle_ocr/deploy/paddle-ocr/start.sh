#!/usr/bin/env bash
# Start and manage the persistent PaddleOCR-VL PDF/image parse service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
REPOSITORY_ROOT="$(cd "${PROJECT_ROOT}/../.." && pwd)"
ENV_FILE="${PADDLE_OCR_ENV_FILE:-${SCRIPT_DIR}/config.env}"
REPO_DOTENV="${PADDLE_OCR_DOTENV:-${REPOSITORY_ROOT}/.env}"

if [[ -f "$ENV_FILE" ]]; then
  if grep -qE '^[[:space:]]*OSS_(ENDPOINT|ACCESS_KEY_ID|ACCESS_KEY_SECRET|BUCKET_NAME)=' "$ENV_FILE"; then
    echo "Warning: OSS credentials in ${ENV_FILE} are ignored; put them in ${REPO_DOTENV}" >&2
  fi
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

unset OSS_ENDPOINT OSS_ACCESS_KEY_ID OSS_ACCESS_KEY_SECRET OSS_BUCKET_NAME \
  OSS_PREFIX OSS_SIGNED_URL_EXPIRES_SECONDS
if [[ -f "$REPO_DOTENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$REPO_DOTENV"
  set +a
fi
# Keep paddle results separate from glm-ocr-v2 unless explicitly overridden.
export OSS_PREFIX="${PADDLE_OCR_OSS_PREFIX:-paddle_ocr_output}"

CONDA_BASE="${PADDLE_OCR_CONDA_BASE:-/data/wilson_2/conda}"
CONDA_ENV="${PADDLE_OCR_CONDA_ENV:-paddle_ppu}"
PADDLE_OCR_PYTHON="${PADDLE_OCR_PYTHON:-${CONDA_BASE}/envs/${CONDA_ENV}/bin/python}"
PADDLE_OCR_SERVICE_CONFIG="${PADDLE_OCR_SERVICE_CONFIG:-${PROJECT_ROOT}/config/ocr_services.yaml}"
PADDLE_OCR_HOST="${PADDLE_OCR_HOST:-127.0.0.1}"
PADDLE_OCR_PORT="${PADDLE_OCR_PORT:-18093}"
PADDLE_OCR_OUTPUT_ROOT="${PADDLE_OCR_OUTPUT_ROOT:-${REPOSITORY_ROOT}/result/paddle_ocr/framework}"
PADDLE_OCR_LOG_DIR="${PADDLE_OCR_LOG_DIR:-${PROJECT_ROOT}/logs}"
PADDLE_OCR_LOG_FILE="${PADDLE_OCR_LOG_FILE:-${PADDLE_OCR_LOG_DIR}/paddle_ocr_service.log}"
PADDLE_OCR_PID_FILE="${PADDLE_OCR_PID_FILE:-${PADDLE_OCR_LOG_DIR}/paddle_ocr_service.pid}"
PADDLEX_HOME="${PADDLEX_HOME:-/data/wilson_2/.paddlex}"
CUDNN_HOME="${CUDNN_HOME:-/usr/local/PPU_SDK/CUDA_SDK}"
PPU_SDK="${PPU_SDK:-/usr/local/PPU_SDK}"

usage() {
  echo "Usage: $(basename "$0") [--foreground|--status|--stop]"
}

running() {
  [[ -f "$PADDLE_OCR_PID_FILE" ]] || return 1
  local pid state
  pid="$(cat "$PADDLE_OCR_PID_FILE" 2>/dev/null)"
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
      echo "PaddleOCR running: PID $(cat "$PADDLE_OCR_PID_FILE"), port=$PADDLE_OCR_PORT"
    else
      echo "PaddleOCR stopped"
    fi
    exit 0
    ;;
  --stop)
    if running; then
      kill "$(cat "$PADDLE_OCR_PID_FILE")"
      echo "Stopped PaddleOCR"
    fi
    rm -f "$PADDLE_OCR_PID_FILE"
    exit 0
    ;;
  --foreground|"")
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

[[ -x "$PADDLE_OCR_PYTHON" ]] || {
  echo "PaddleOCR Python not executable: $PADDLE_OCR_PYTHON" >&2
  exit 1
}
[[ -f "$PADDLE_OCR_SERVICE_CONFIG" ]] || {
  echo "PaddleOCR service config not found: $PADDLE_OCR_SERVICE_CONFIG" >&2
  exit 1
}
running && {
  echo "PaddleOCR already running: PID $(cat "$PADDLE_OCR_PID_FILE")" >&2
  exit 1
}

mkdir -p "$PADDLE_OCR_LOG_DIR" "$PADDLE_OCR_OUTPUT_ROOT"
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
export PADDLEX_HOME CUDNN_HOME
export LD_LIBRARY_PATH="${PPU_SDK}/lib:${CUDNN_HOME}/lib64:${LD_LIBRARY_PATH:-}"
# Prefer paddle libs from shared PPU stack if present.
for _d in \
  "${CONDA_BASE}/envs/${CONDA_ENV}/lib/python3.12/site-packages/paddle/libs" \
  /data/wilson_2/de/paddle_ocr/.venv/lib/python3.12/site-packages/paddle/libs
do
  if [[ -d "$_d" ]]; then
    export LD_LIBRARY_PATH="${_d}:${LD_LIBRARY_PATH}"
    break
  fi
done
export PYTHONUNBUFFERED=1
export TMPDIR="${TMPDIR:-/data/wilson_2/tmp}"

cmd=(
  "$PADDLE_OCR_PYTHON"
  "${PROJECT_ROOT}/services/paddle_ocr/service.py"
  --service-config "$PADDLE_OCR_SERVICE_CONFIG"
)

if [[ "${1:-}" == "--foreground" ]]; then
  exec "${cmd[@]}"
fi

nohup "${cmd[@]}" >>"$PADDLE_OCR_LOG_FILE" 2>&1 &
pid=$!
echo "$pid" >"$PADDLE_OCR_PID_FILE"
echo "PaddleOCR started: PID $pid (${PADDLE_OCR_HOST}:${PADDLE_OCR_PORT})"
echo "Output: $PADDLE_OCR_OUTPUT_ROOT"
echo "Log: $PADDLE_OCR_LOG_FILE"
