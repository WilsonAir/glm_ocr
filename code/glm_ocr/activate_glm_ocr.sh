#!/usr/bin/env bash
# Activate glm_ocr environment (pip venv on PPU ac2 stack)
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export GLM_OCR_HOME="$PROJECT_ROOT"
export GLMOCR_CONFIG="$PROJECT_ROOT/config.yaml"
export VIRTUAL_ENV="$PROJECT_ROOT/.venv"
export PATH="$VIRTUAL_ENV/bin:/opt/ac2/bin:$PATH"
# Layout on GPU0; vLLM GLM-OCR typically on GPU2 (CUDA_VISIBLE_DEVICES=2)
export GLMOCR_LAYOUT_DEVICE="${GLMOCR_LAYOUT_DEVICE:-cuda:0}"
unset PIP_INDEX_URL

# shellcheck disable=SC1091
if [[ -f "$VIRTUAL_ENV/bin/activate" ]]; then
  source "$VIRTUAL_ENV/bin/activate"
fi

echo "glm_ocr env active (Python: $(which python), torch via ac2 PPU)"
