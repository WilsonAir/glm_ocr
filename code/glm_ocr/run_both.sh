#!/usr/bin/env bash
# Run both OCR modes on the sample PDF and save to result/.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INPUT="${INPUT:-${PDF:-/data/wilson_2/de/medical_paper_catalog/sample_pdfs/aml_nccn.pdf}}"
API_BASE="${API_BASE:-http://127.0.0.1:18080/v1}"
PYTHON="${PYTHON:-/opt/ac2/bin/python3}"
MAX_PAGES="${MAX_PAGES:-}"

echo "=== [1/2] Model-only inference (vLLM API, no layout SDK) ==="
MODEL_OUT="$PROJECT_ROOT/result/model_only"
mkdir -p "$MODEL_OUT"
EXTRA=()
if [[ -n "$MAX_PAGES" ]]; then
  EXTRA+=(--max-pages "$MAX_PAGES")
fi
"$PYTHON" /data/wilson_2/de/OCR_Test/test_ocr.py \
  --pdf "$INPUT" \
  --result-dir "$MODEL_OUT" \
  --api-base "$API_BASE" \
  "${EXTRA[@]}"

echo ""
echo "=== [2/2] Framework inference (glmocr SDK + layout + vLLM) ==="
FRAMEWORK_OUT="$PROJECT_ROOT/result/framework"
source "$PROJECT_ROOT/activate_glm_ocr.sh"
EXTRA_SDK=()
if [[ -n "$MAX_PAGES" ]]; then
  EXTRA_SDK+=(--max-pages "$MAX_PAGES")
fi
python "$PROJECT_ROOT/test_sdk_parse.py" \
  --input "$INPUT" \
  --output "$FRAMEWORK_OUT" \
  "${EXTRA_SDK[@]}"

echo ""
echo "Done."
echo "  Model-only:  $MODEL_OUT"
echo "  Framework:   $FRAMEWORK_OUT"
