#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$PROJECT_ROOT/activate_paddle_ocr.sh"

export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
export PADDLEX_HOME="${PADDLEX_HOME:-/data/wilson_2/.paddlex}"
export CUDNN_HOME="${CUDNN_HOME:-/usr/local/PPU_SDK/CUDA_SDK}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export LD_LIBRARY_PATH="${VIRTUAL_ENV}/lib/python3.12/site-packages/paddle/libs:/usr/local/PPU_SDK/lib:/usr/local/PPU_SDK/CUDA_SDK/lib64:${LD_LIBRARY_PATH:-}"

PDF="${PDF:-/data/wilson_2/de/medical_paper_catalog/sample_pdfs/aml_nccn.pdf}"
OUTPUT="${OUTPUT:-$PROJECT_ROOT/result/framework}"
DEVICE="${DEVICE:-cpu}"
VLLM_URL="${VLLM_URL:-http://127.0.0.1:18081/v1}"
VLLM_MODEL="${VLLM_MODEL:-paddleocr-vl-1.6}"
VLLM_MAX_CONCURRENCY="${VLLM_MAX_CONCURRENCY:-8}"

exec python "$PROJECT_ROOT/test_framework.py" \
  --pdf "$PDF" \
  --output "$OUTPUT" \
  --vllm-url "$VLLM_URL" \
  --vllm-model "$VLLM_MODEL" \
  --vllm-max-concurrency "$VLLM_MAX_CONCURRENCY" \
  --device "$DEVICE"
