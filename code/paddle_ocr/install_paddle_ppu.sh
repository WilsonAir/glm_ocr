#!/usr/bin/env bash
# Install paddlepaddle-gpu for PAI-PPU SDK 2.0.x (match torch ppu2.0.0 stack).
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$PROJECT_ROOT/.venv}"
AC2_PYTHON="${AC2_PYTHON:-/opt/ac2/bin/python3}"
# PPU SDK 2.0.0 -> use ppu2.1.0.ce wheel (ppu1.7.0 GPU kernels segfault on PPU 2.0)
PADDLE_VERSION="${PADDLE_VERSION:-3.2.2+v0.1.0.ppu2.1.0.ce}"
WHEEL_URL="${WHEEL_URL:-https://aiext-pypi.mirrors.aliyuncs.com/pg1-pip/generic/paddlepaddle_gpu/3.2.2+v0.1.0.ppu2.1.0.ce/paddlepaddle_gpu-3.2.2+cu129ubuntu2404ce-cp312-cp312-linux_x86_64.whl}"
WHEEL_CACHE="${WHEEL_CACHE:-/data/wilson_2/.pip-cache/paddlepaddle_gpu-3.2.2+cu129ubuntu2404ce-cp312-cp312-linux_x86_64.ppu2.1.0.whl}"

export TMPDIR="${TMPDIR:-/data/wilson_2/tmp}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-/data/wilson_2/.pip-cache}"
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$AC2_PYTHON" -m venv "$VENV_DIR" --system-site-packages
fi

PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python"

if [[ ! -f "$WHEEL_CACHE" ]]; then
  echo "Downloading paddlepaddle-gpu ($PADDLE_VERSION) for PPU 2.0..."
  curl -L --progress-bar -o "$WHEEL_CACHE" "$WHEEL_URL"
fi

echo "Installing $WHEEL_CACHE ..."
"$PIP" uninstall -y paddlepaddle-gpu paddlepaddle 2>/dev/null || true
"$PIP" install "$WHEEL_CACHE" --force-reinstall --no-deps

export LD_LIBRARY_PATH="$VENV_DIR/lib/python3.12/site-packages/paddle/libs:/usr/local/PPU_SDK/lib:/usr/local/PPU_SDK/CUDA_SDK/lib64:${LD_LIBRARY_PATH:-}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
"$PYTHON" -c "
import paddle
paddle.set_device('gpu:0')
x = paddle.randn([2, 3])
print('paddle', paddle.__version__, 'GPU OK on', x.place)
"
