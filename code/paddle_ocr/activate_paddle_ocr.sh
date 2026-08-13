#!/usr/bin/env bash
# Activate conda env paddle_ocr for PaddleOCR-VL.
export PADDLE_OCR_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONDA_BASE="${CONDA_BASE:-/data/wilson_2/soft/miniforge3}"
CONDA_ENV_NAME="${CONDA_ENV_NAME:-paddle_ocr}"

if [[ ! -f "$CONDA_BASE/etc/profile.d/conda.sh" ]]; then
  echo "ERROR: conda not found at $CONDA_BASE" >&2
  return 1 2>/dev/null || exit 1
fi

# shellcheck source=/dev/null
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV_NAME"

export TMPDIR="${TMPDIR:-/data/wilson_2/tmp}"
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-/data/wilson_2/.pip-cache}"

# Paddle native libs + PPU SDK (paddle may live in env or shared PPU stack).
_PADDLE_LIBS=""
for _d in \
  "${CONDA_PREFIX}/lib/python3.12/site-packages/paddle/libs" \
  /data/wilson_2/de/paddle_ocr/.venv/lib/python3.12/site-packages/paddle/libs
do
  if [[ -d "$_d" ]]; then
    _PADDLE_LIBS="$_d"
    break
  fi
done
export LD_LIBRARY_PATH="${_PADDLE_LIBS:+$_PADDLE_LIBS:}/usr/local/PPU_SDK/lib:/usr/local/PPU_SDK/CUDA_SDK/lib64:${LD_LIBRARY_PATH:-}"
unset _PADDLE_LIBS _d

echo "paddle_ocr env active ($(which python) / ${CONDA_DEFAULT_ENV:-?})"
