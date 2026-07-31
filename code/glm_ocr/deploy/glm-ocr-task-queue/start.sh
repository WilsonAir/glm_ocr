#!/usr/bin/env bash
# Start and manage the outer GLM-OCR persistent task queue.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${GLM_OCR_TASK_ENV_FILE:-${SCRIPT_DIR}/config.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

GLM_OCR_TASK_PYTHON="${GLM_OCR_TASK_PYTHON:-/data/wilson_2/conda/envs/med_rag_cuda/bin/python}"
GLM_OCR_TASK_CONFIG="${GLM_OCR_TASK_CONFIG:-${PROJECT_ROOT}/config/task_queue_v2.yaml}"
GLM_OCR_TASK_HOST="${GLM_OCR_TASK_HOST:-127.0.0.1}"
GLM_OCR_TASK_PORT="${GLM_OCR_TASK_PORT:-18092}"
GLM_OCR_TASK_LOG_DIR="${GLM_OCR_TASK_LOG_DIR:-${PROJECT_ROOT}/logs}"
GLM_OCR_TASK_LOG_FILE="${GLM_OCR_TASK_LOG_FILE:-${GLM_OCR_TASK_LOG_DIR}/task_queue.log}"
GLM_OCR_TASK_PID_FILE="${GLM_OCR_TASK_PID_FILE:-${GLM_OCR_TASK_LOG_DIR}/task_queue.pid}"

usage() {
  echo "Usage: $(basename "$0") [--foreground|--status|--stop]"
}

running() {
  [[ -f "$GLM_OCR_TASK_PID_FILE" ]] || return 1
  local pid state
  pid="$(cat "$GLM_OCR_TASK_PID_FILE" 2>/dev/null)"
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
      echo "GLM-OCR task queue running: PID $(cat "$GLM_OCR_TASK_PID_FILE"), port=$GLM_OCR_TASK_PORT"
    else
      echo "GLM-OCR task queue stopped"
    fi
    exit 0
    ;;
  --stop)
    if running; then
      kill "$(cat "$GLM_OCR_TASK_PID_FILE")"
      echo "Stopped GLM-OCR task queue"
    fi
    rm -f "$GLM_OCR_TASK_PID_FILE"
    exit 0
    ;;
  --foreground|"")
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

[[ -x "$GLM_OCR_TASK_PYTHON" ]] || {
  echo "Task queue Python not executable: $GLM_OCR_TASK_PYTHON" >&2
  exit 1
}
[[ -f "$GLM_OCR_TASK_CONFIG" ]] || {
  echo "Task queue config not found: $GLM_OCR_TASK_CONFIG" >&2
  exit 1
}
running && {
  echo "GLM-OCR task queue already running: PID $(cat "$GLM_OCR_TASK_PID_FILE")" >&2
  exit 1
}

mkdir -p "$GLM_OCR_TASK_LOG_DIR"
export PYTHONUNBUFFERED=1

cmd=(
  "$GLM_OCR_TASK_PYTHON"
  "${PROJECT_ROOT}/services/glm_ocr_task_queue/service.py"
  --config "$GLM_OCR_TASK_CONFIG"
)

if [[ "${1:-}" == "--foreground" ]]; then
  exec "${cmd[@]}"
fi

nohup "${cmd[@]}" >>"$GLM_OCR_TASK_LOG_FILE" 2>&1 &
pid=$!
echo "$pid" >"$GLM_OCR_TASK_PID_FILE"
echo "GLM-OCR task queue started: PID $pid (${GLM_OCR_TASK_HOST}:${GLM_OCR_TASK_PORT})"
echo "Log: $GLM_OCR_TASK_LOG_FILE"
