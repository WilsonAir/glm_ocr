#!/usr/bin/env python3
"""PaddleOCR-VL via vLLM OpenAI API (model-only, no PaddleOCR pipeline)."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import fitz
import requests

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_PDF = Path("/data/wilson_2/de/medical_paper_catalog/sample_pdfs/aml_nccn.pdf")
DEFAULT_RESULT_DIR = PROJECT_ROOT / "result" / "model_only"
DEFAULT_API_BASE = "http://127.0.0.1:18081/v1"
DEFAULT_MODEL = "paddleocr-vl-1.6"
DEFAULT_PROMPT = "OCR:"


def pdf_to_images(pdf_path: Path, images_dir: Path, dpi: int = 200) -> list[Path]:
    images_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    image_paths: list[Path] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    for page_index in range(doc.page_count):
        page = doc.load_page(page_index)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        out_path = images_dir / f"page_{page_index + 1:03d}.png"
        pix.save(out_path)
        image_paths.append(out_path)
    doc.close()
    return image_paths


def ocr_image(api_base: str, model: str, image_path: Path, prompt: str, timeout: int = 600) -> dict:
    url = f"{api_base.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"file://{image_path.resolve()}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": 2048,
        "temperature": 0.0,
    }
    started = time.perf_counter()
    response = requests.post(url, json=payload, timeout=timeout)
    elapsed = time.perf_counter() - started
    if response.status_code != 200:
        raise RuntimeError(f"OCR API error {response.status_code}: {response.text[:500]}")
    data = response.json()
    return {
        "text": data["choices"][0]["message"]["content"],
        "elapsed_sec": round(elapsed, 2),
        "usage": data.get("usage", {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="PaddleOCR-VL model-only test via vLLM")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--result-dir", type=Path, default=DEFAULT_RESULT_DIR)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()

    if not args.pdf.is_file():
        raise SystemExit(f"PDF not found: {args.pdf}")

    resp = requests.get(f"{args.api_base.rstrip('/')}/models", timeout=10)
    if resp.status_code != 200:
        raise SystemExit(f"API unhealthy: {resp.status_code}")

    result_dir = args.result_dir.resolve()
    result_dir.mkdir(parents=True, exist_ok=True)
    image_paths = pdf_to_images(args.pdf, result_dir / "pages", dpi=args.dpi)
    if args.max_pages is not None:
        image_paths = image_paths[:args.max_pages]

    markdown_parts = [
        f"# PaddleOCR-VL Model-Only: {args.pdf.name}",
        "",
        f"- API: `{args.api_base}`",
        f"- Model: `{args.model}`",
        f"- Prompt: `{args.prompt}`",
        f"- Pages: {len(image_paths)}",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    page_results = []

    for idx, image_path in enumerate(image_paths, start=1):
        print(f"[{idx}/{len(image_paths)}] OCR {image_path.name} ...")
        result = ocr_image(args.api_base, args.model, image_path, args.prompt)
        text = result["text"].strip()
        (result_dir / f"page_{idx:03d}.txt").write_text(text, encoding="utf-8")
        page_results.append({"page": idx, "elapsed_sec": result["elapsed_sec"], "usage": result["usage"]})
        markdown_parts.extend([f"## Page {idx}", "", text, ""])
        print(f"  done in {result['elapsed_sec']}s")

    md_path = result_dir / "aml_nccn.md"
    md_path.write_text("\n".join(markdown_parts), encoding="utf-8")
    summary = {
        "mode": "model_only_vllm",
        "pdf": str(args.pdf),
        "api_base": args.api_base,
        "model": args.model,
        "page_count": len(image_paths),
        "markdown": str(md_path),
        "pages": page_results,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    (result_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    main()
