#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
REPOSITORY_ROOT="$(cd "${PROJECT_ROOT}/../.." && pwd)"
ENV_FILE="${GLM_OCR_V4_ENV_FILE:-${SCRIPT_DIR}/config.env}"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

PYTHON="${GLM_OCR_V4_PYTHON:-${GLM_OCR_V4_CONDA_BASE:-/data/wilson_2/soft/miniforge3}/envs/${GLM_OCR_V4_CONDA_ENV:-med_rag_cuda}/bin/python}"
SERVICE_CONFIG="${GLM_OCR_V4_SERVICE_CONFIG:-${PROJECT_ROOT}/config/ocr_services_v4.yaml}"
SDK_CONFIG="${GLM_OCR_V4_CONFIG:-${PROJECT_ROOT}/config/glm_ocr_v4_zh.yaml}"
HOST="${GLM_OCR_V4_HOST:-127.0.0.1}"
PORT="${GLM_OCR_V4_PORT:-18095}"
DEVICE="${GLM_OCR_V4_LAYOUT_DEVICE:-cuda:1}"
OUTPUT="${GLM_OCR_V4_OUTPUT_ROOT:-${REPOSITORY_ROOT}/result/glm_ocr/framework_v4_zh}"

[[ -x "$PYTHON" ]] || { echo "V4 Python not executable: $PYTHON" >&2; exit 1; }
mkdir -p "$OUTPUT"
exec "$PYTHON" "$PROJECT_ROOT/services/glm_ocr_v4/service.py" \
  --service-config "$SERVICE_CONFIG" --host "$HOST" --port "$PORT" \
  --config "$SDK_CONFIG" --layout-device "$DEVICE" --output-root "$OUTPUT"
