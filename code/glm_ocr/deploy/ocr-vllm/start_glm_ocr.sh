#!/usr/bin/env bash
# Start GLM-OCR vLLM on one explicitly selected GPU.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${GLM_OCR_VLLM_ENV_FILE:-${SCRIPT_DIR}/config.env}"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

VLLM_BIN="${VLLM_BIN:-/opt/ac2/bin/vllm}"
CUDA_PATH="${CUDA_PATH:-/usr/local/PPU_SDK/CUDA_SDK}"
GPU="${GLM_OCR_VLLM_DEVICE:-2}"
PORT="${GLM_OCR_VLLM_PORT:-18080}"
HOST="${OCR_VLLM_HOST:-0.0.0.0}"
MODEL="${GLM_OCR_MODEL_PATH:-/data/wilson_2/de/models/ZhipuAI/GLM-OCR}"
MODEL_NAME="${GLM_OCR_VLLM_MODEL_NAME:-glm-ocr}"
GPU_MEM="${OCR_VLLM_GPU_MEMORY_UTILIZATION:-0.3}"
MAX_LEN="${OCR_VLLM_MAX_MODEL_LEN:-8192}"
LOG_DIR="${OCR_VLLM_LOG_DIR:-${PROJECT_ROOT}/logs}"
LOG_FILE="${GLM_OCR_VLLM_LOG_FILE:-${LOG_DIR}/glm_ocr_vllm.log}"
PID_FILE="${GLM_OCR_VLLM_PID_FILE:-${LOG_DIR}/glm_ocr_vllm.pid}"

usage() { echo "Usage: $(basename "$0") [--foreground|--stop|--status]"; }
die() { echo "[glm-ocr-vllm] ERROR: $*" >&2; exit 1; }
running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid state
  pid="$(cat "$PID_FILE" 2>/dev/null)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  state="$(awk '/^State:/{print $2}' "/proc/${pid}/status" 2>/dev/null || true)"
  [[ "$state" != "Z" ]]
}

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then usage; exit 0; fi
if [[ "${1:-}" == --status ]]; then
  running && echo "glm-ocr vLLM running: PID $(cat "$PID_FILE"), GPU=$GPU, port=$PORT" || echo "glm-ocr vLLM stopped"
  exit 0
fi
if [[ "${1:-}" == --stop ]]; then
  if running; then kill "$(cat "$PID_FILE")"; echo "stopped glm-ocr vLLM"; fi
  rm -f "$PID_FILE"; exit 0
fi

[[ -x "$VLLM_BIN" ]] || die "vLLM executable not found: $VLLM_BIN (set VLLM_BIN)"
[[ -d "$MODEL" ]] || die "model directory not found: $MODEL"
[[ -d "$CUDA_PATH" ]] || die "CUDA SDK directory not found: $CUDA_PATH (set CUDA_PATH)"
[[ -x "$CUDA_PATH/bin/ptxas" ]] || die "ptxas is not executable: $CUDA_PATH/bin/ptxas"
running && die "already running: PID $(cat "$PID_FILE")"
mkdir -p "$LOG_DIR"

# Use the PPU CUDA SDK explicitly. Ignore user-site .pth files that may shadow
# the compiled /usr/local vLLM package with a source checkout such as /opt/vllm.
export CUDA_PATH
export PATH="$CUDA_PATH/bin:$PATH"
export PYTHONNOUSERSITE="${PYTHONNOUSERSITE:-1}"
# VLLM_BIN is a launcher-only variable; vLLM warns about unknown exported VLLM_* vars.
export -n VLLM_BIN 2>/dev/null || true

cmd=("$VLLM_BIN" serve "$MODEL" --served-model-name "$MODEL_NAME"
  --host "$HOST" --port "$PORT" --allowed-local-media-path /
  --gpu-memory-utilization "$GPU_MEM" --max-model-len "$MAX_LEN")

if [[ "${1:-}" == --foreground ]]; then
  exec env CUDA_VISIBLE_DEVICES="$GPU" "${cmd[@]}"
fi
nohup env CUDA_VISIBLE_DEVICES="$GPU" "${cmd[@]}" >>"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
echo "glm-ocr vLLM started: PID $!, GPU=$GPU, port=$PORT, log=$LOG_FILE"
