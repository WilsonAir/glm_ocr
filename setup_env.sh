#!/usr/bin/env bash
# glm_ocr Python environment (PPU / ac2 base + pip venv)
# Replaces broken conda env — uses Alibaba PPU pip + /opt/ac2 PyTorch stack.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
AC2_PYTHON="/opt/ac2/bin/python3"
PPU_PIP_INDEX_CU129="https://aiext-pypi.mirrors.aliyuncs.com/pg1-pip/cu129/simple/"

unset PIP_INDEX_URL

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Creating venv from ac2 Python (system-site-packages for PPU torch)..."
  "$AC2_PYTHON" -m venv "$VENV_DIR" --system-site-packages
fi

PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python"

echo "Installing glmocr SDK..."
"$PIP" install glmocr pypdfium2 -q

echo "Verifying..."
"$PYTHON" -c "import glmocr, torch; print('glmocr OK, torch', torch.__version__)"

cat > "$PROJECT_ROOT/pip.conf.ppu" <<EOF
# Alibaba PPU PIP (CUDA 12.9) — use when installing extra packages:
#   pip install <pkg> -c $PROJECT_ROOT/pip.conf.ppu
[global]
index-url = $PPU_PIP_INDEX_CU129
[install]
trusted-host = aiext-pypi.mirrors.aliyuncs.com
EOF

echo "Done. Activate with: source $PROJECT_ROOT/activate_glm_ocr.sh"
