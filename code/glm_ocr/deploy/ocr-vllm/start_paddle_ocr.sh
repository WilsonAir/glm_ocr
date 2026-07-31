#!/usr/bin/env bash
# Start GLM-OCR vLLM on one explicitly selected GPU.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${PADDLE_OCR_VLLM_ENV_FILE:-${SCRIPT_DIR}/config.env}"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

VLLM_BIN="${VLLM_BIN:-/opt/ac2/bin/vllm}"
GPU="${PADDLE_OCR_VLLM_DEVICE:-1}"
PORT="${PADDLE_OCR_VLLM_PORT:-18081}"
HOST="${OCR_VLLM_HOST:-0.0.0.0}"
MODEL="${PADDLE_OCR_MODEL_PATH:-/data/wilson_2/de/models/PaddlePaddle/PaddleOCR-VL-1___6}"
MODEL_NAME="${GLM_OCR_VLLM_MODEL_NAME:-paddle-ocr}"
GPU_MEM="${OCR_VLLM_GPU_MEMORY_UTILIZATION:-0.3}"
MAX_LEN="${OCR_VLLM_MAX_MODEL_LEN:-8192}"
LOG_DIR="${OCR_VLLM_LOG_DIR:-${PROJECT_ROOT}/logs}"
LOG_FILE="${GLM_OCR_VLLM_LOG_FILE:-${LOG_DIR}/paddle_ocr_vllm.log}"
PID_FILE="${GLM_OCR_VLLM_PID_FILE:-${LOG_DIR}/paddle_ocr_vllm.pid}"

usage() { echo "Usage: $(basename "$0") [--foreground|--stop|--status]"; }
die() { echo "[paddle-ocr-vllm] ERROR: $*" >&2; exit 1; }
running() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then usage; exit 0; fi
if [[ "${1:-}" == --status ]]; then
  running && echo "paddle-ocr vLLM running: PID $(cat "$PID_FILE"), GPU=$GPU, port=$PORT" || echo "paddle-ocr vLLM stopped"
  exit 0
fi
if [[ "${1:-}" == --stop ]]; then
  if running; then kill "$(cat "$PID_FILE")"; echo "stopped paddle-ocr vLLM"; fi
  rm -f "$PID_FILE"; exit 0
fi

[[ -x "$VLLM_BIN" ]] || die "vLLM executable not found: $VLLM_BIN (set VLLM_BIN)"
[[ -d "$MODEL" ]] || die "model directory not found: $MODEL"
running && die "already running: PID $(cat "$PID_FILE")"
mkdir -p "$LOG_DIR"
cmd=("$VLLM_BIN" serve "$MODEL" --served-model-name "$MODEL_NAME"
  --host "$HOST" --port "$PORT" --allowed-local-media-path /
  --gpu-memory-utilization "$GPU_MEM" --max-model-len "$MAX_LEN"
  --trust-remote-code)

if [[ "${1:-}" == --foreground ]]; then
  exec env CUDA_VISIBLE_DEVICES="$GPU" "${cmd[@]}"
fi
nohup env CUDA_VISIBLE_DEVICES="$GPU" "${cmd[@]}" >>"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
echo "paddle-ocr vLLM started: PID $!, GPU=$GPU, port=$PORT, log=$LOG_FILE"
