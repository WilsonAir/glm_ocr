#!/usr/bin/env bash
export PADDLE_OCR_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export VIRTUAL_ENV="$PADDLE_OCR_HOME/.venv"
export PATH="$VIRTUAL_ENV/bin:/opt/ac2/bin:$PATH"
export TMPDIR="${TMPDIR:-/data/wilson_2/tmp}"
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-/data/wilson_2/.pip-cache}"
export LD_LIBRARY_PATH="${VIRTUAL_ENV}/lib/python3.12/site-packages/paddle/libs:/usr/local/PPU_SDK/lib:/usr/local/PPU_SDK/CUDA_SDK/lib64:${LD_LIBRARY_PATH:-}"
if [[ -f "$VIRTUAL_ENV/bin/activate" ]]; then
  source "$VIRTUAL_ENV/bin/activate"
fi
echo "paddle_ocr env active ($(which python))"
