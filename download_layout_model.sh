#!/usr/bin/env bash
# Download PP-DocLayoutV3 layout model via ModelScope (no HuggingFace required).
set -euo pipefail

PYTHON="${PYTHON:-/opt/ac2/bin/python3}"
OUT_DIR="${OUT_DIR:-/data/wilson_2/de/models}"

"$PYTHON" - <<'PY'
from modelscope import snapshot_download

path = snapshot_download(
    "PaddlePaddle/PP-DocLayoutV3_safetensors",
    cache_dir="/data/wilson_2/de/models",
)
print(f"Layout model ready: {path}")
PY
