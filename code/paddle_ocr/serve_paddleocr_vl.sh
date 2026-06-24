#!/usr/bin/env bash
# PaddleOCR-VL vLLM OpenAI-compatible server (requires --trust-remote-code).
set -euo pipefail

MODEL_PATH="${MODEL_PATH:-/data/wilson_2/de/models/PaddlePaddle/PaddleOCR-VL-1___6}"
SERVED_NAME="${SERVED_NAME:-paddleocr-vl-1.6}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-18081}"
CUDA_DEVICE="${CUDA_DEVICE:-1}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.85}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"
VLLM_BIN="${VLLM_BIN:-/opt/ac2/bin/vllm}"

export CUDA_VISIBLE_DEVICES="$CUDA_DEVICE"

exec "$VLLM_BIN" serve "$MODEL_PATH" \
  --served-model-name "$SERVED_NAME" \
  --host "$HOST" \
  --port "$PORT" \
  --allowed-local-media-path / \
  --gpu-memory-utilization "$GPU_MEM_UTIL" \
  --max-model-len "$MAX_MODEL_LEN" \
  --trust-remote-code
