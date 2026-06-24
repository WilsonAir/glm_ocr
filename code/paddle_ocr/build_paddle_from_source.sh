#!/usr/bin/env bash
# Fallback: compile PaddlePaddle GPU from source on PAI-PPU (Aliyun 3.4).
# All build artifacts go to /data to avoid filling root disk.
set -euo pipefail

SRC_ROOT="${SRC_ROOT:-/data/wilson_2/src/paddle}"
BUILD_DIR="${BUILD_DIR:-/data/wilson_2/build/paddle}"
VENV_DIR="${VENV_DIR:-/data/wilson_2/de/paddle_ocr/.venv}"
PADDLE_BRANCH="${PADDLE_BRANCH:-release/3.2}"
PY_BIN="${PY_BIN:-/opt/ac2/bin/python3}"
JOBS="${JOBS:-$(nproc)}"

export TMPDIR="${TMPDIR:-/data/wilson_2/tmp}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-/data/wilson_2/.pip-cache}"
unset PIP_INDEX_URL
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR" "$SRC_ROOT" "$BUILD_DIR"

PY_VERSION="$("$PY_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

if [[ ! -d "$SRC_ROOT/.git" ]]; then
  echo "Cloning Paddle ($PADDLE_BRANCH)..."
  git clone --depth=1 -b "$PADDLE_BRANCH" https://github.com/PaddlePaddle/Paddle.git "$SRC_ROOT"
fi

cd "$SRC_ROOT"
git submodule update --init --depth=1 cmake external 2>/dev/null || true

"$PY_BIN" -m pip install -r "$SRC_ROOT/python/requirements.txt" \
  --cache-dir "$PIP_CACHE_DIR" -q

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

echo "Configuring cmake (PY_VERSION=$PY_VERSION, WITH_GPU=ON)..."
cmake "$SRC_ROOT" \
  -DPY_VERSION="$PY_VERSION" \
  -DWITH_GPU=ON \
  -DWITH_DISTRIBUTE=OFF \
  -DCMAKE_BUILD_TYPE=Release

echo "Building ($JOBS jobs) — this may take 1-2 hours..."
cmake --build . -j"$JOBS"

WHEEL="$(ls "$BUILD_DIR"/python/dist/paddlepaddle_gpu-*.whl | head -1)"
echo "Installing $WHEEL into venv..."
"$VENV_DIR/bin/pip" install "$WHEEL" --force-reinstall

export LD_LIBRARY_PATH="$VENV_DIR/lib/python3.12/site-packages/paddle/libs:${LD_LIBRARY_PATH:-}"
"$VENV_DIR/bin/python" -c "import paddle; print('paddle from source:', paddle.__version__)"
