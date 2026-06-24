#!/usr/bin/env bash
# PaddleOCR env on /data (root disk is full — cache/tmp on NAS).
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
AC2_PYTHON="/opt/ac2/bin/python3"
export TMPDIR="${TMPDIR:-/data/wilson_2/tmp}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-/data/wilson_2/.pip-cache}"
unset PIP_INDEX_URL

mkdir -p "$TMPDIR" "$PIP_CACHE_DIR"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$AC2_PYTHON" -m venv "$VENV_DIR" --system-site-packages
fi

PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python"

echo "Installing paddlepaddle-gpu + paddleocr (cache: $PIP_CACHE_DIR)..."
"$PIP" install paddlepaddle-gpu \
  -i https://aiext-pypi.mirrors.aliyuncs.com/pg1-pip/cu129/simple/ \
  --cache-dir "$PIP_CACHE_DIR" || {
  echo "WARN: paddlepaddle-gpu install failed; framework mode may not work."
}

"$PIP" install "paddleocr[doc-parser]>=3.6.0" pymupdf requests \
  --cache-dir "$PIP_CACHE_DIR" || {
  echo "WARN: paddleocr install failed."
}

"$PIP" install pymupdf requests -q --cache-dir "$PIP_CACHE_DIR"

"$PYTHON" -c "import requests, fitz; print('base deps OK')"
echo "Done. Activate: source $PROJECT_ROOT/activate_paddle_ocr.sh"
