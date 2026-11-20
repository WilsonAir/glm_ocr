#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
REPOSITORY_ROOT="$(cd "${PROJECT_ROOT}/../.." && pwd)"
ENV_FILE="${GLM_OCR_V5_ENV_FILE:-${SCRIPT_DIR}/config.env}"
REPO_DOTENV="${GLM_OCR_V5_DOTENV:-${REPOSITORY_ROOT}/.env}"

if [[ -f "$ENV_FILE" ]]; then
  if grep -qE '^[[:space:]]*OSS_(ENDPOINT|ACCESS_KEY_ID|ACCESS_KEY_SECRET|BUCKET_NAME)=' "$ENV_FILE"; then
    echo "Warning: OSS credentials in ${ENV_FILE} are ignored; put them in ${REPO_DOTENV}" >&2
  fi
fi
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

# OSS credentials are loaded only from the gitignored repository-root .env.
unset OSS_ENDPOINT OSS_ACCESS_KEY_ID OSS_ACCESS_KEY_SECRET OSS_BUCKET_NAME \
  OSS_PREFIX OSS_SIGNED_URL_EXPIRES_SECONDS
if [[ -f "$REPO_DOTENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$REPO_DOTENV"
  set +a
fi

PYTHON="${GLM_OCR_V5_PYTHON:-${GLM_OCR_V5_CONDA_BASE:-/data/wilson_2/conda}/envs/${GLM_OCR_V5_CONDA_ENV:-med_rag_cuda}/bin/python}"
SERVICE_CONFIG="${GLM_OCR_V5_SERVICE_CONFIG:-${PROJECT_ROOT}/config/ocr_services_v5.yaml}"
SDK_CONFIG="${GLM_OCR_V5_CONFIG:-${PROJECT_ROOT}/config/glm_ocr.yaml}"
HOST="${GLM_OCR_V5_HOST:-127.0.0.1}"
PORT="${GLM_OCR_V5_PORT:-18096}"
DEVICE="${GLM_OCR_V5_LAYOUT_DEVICE:-cuda:1}"
OUTPUT="${GLM_OCR_V5_OUTPUT_ROOT:-${REPOSITORY_ROOT}/result/glm_ocr/framework_v5}"

[[ -x "$PYTHON" ]] || { echo "V5 Python not executable: $PYTHON" >&2; exit 1; }
mkdir -p "$OUTPUT"
exec "$PYTHON" "$PROJECT_ROOT/services/glm_ocr_v5/service.py" \
  --service-config "$SERVICE_CONFIG" --host "$HOST" --port "$PORT" \
  --config "$SDK_CONFIG" --layout-device "$DEVICE" --output-root "$OUTPUT"
