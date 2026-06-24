#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PDF="${PDF:-/data/wilson_2/de/medical_paper_catalog/sample_pdfs/aml_nccn.pdf}"
API_BASE="${API_BASE:-http://127.0.0.1:18081/v1}"
PYTHON="${PYTHON:-/opt/ac2/bin/python3}"

echo "=== [1/2] PaddleOCR-VL model-only (vLLM @ $API_BASE) ==="
"$PYTHON" "$PROJECT_ROOT/test_model_only.py" --pdf "$PDF" --api-base "$API_BASE"

echo ""
echo "=== [2/2] PaddleOCR-VL framework (doc_parser + vLLM) ==="
if source "$PROJECT_ROOT/activate_paddle_ocr.sh" 2>/dev/null && python -c "import paddleocr" 2>/dev/null; then
  bash "$PROJECT_ROOT/run_framework.sh"
else
  echo "SKIP: paddleocr not available — install with bash setup_env.sh"
  mkdir -p "$PROJECT_ROOT/result/framework"
  echo '{"skipped": true, "reason": "paddleocr not installed"}' > "$PROJECT_ROOT/result/framework/summary.json"
fi

echo "Done. Results in $PROJECT_ROOT/result/"
