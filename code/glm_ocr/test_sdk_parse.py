#!/usr/bin/env python3
"""High-quality PDF parsing via official GLM-OCR SDK + remote vLLM."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from glmocr import GlmOcr

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"
DEFAULT_PDF = Path(
    "/data/wilson_2/de/medical_paper_catalog/sample_pdfs/aml_nccn.pdf"
)
DEFAULT_OUTPUT = PROJECT_ROOT / "result"


def main() -> None:
    parser = argparse.ArgumentParser(description="GLM-OCR SDK parse test (vLLM backend)")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--layout-device",
        default=None,
        help="Layout model device, e.g. cuda:0 or cpu (default from env/config)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit PDF pages via config override",
    )
    args = parser.parse_args()

    if not args.pdf.is_file():
        raise SystemExit(f"PDF not found: {args.pdf}")

    args.output.mkdir(parents=True, exist_ok=True)
    overrides: dict = {"mode": "selfhosted"}
    if args.max_pages is not None:
        overrides["_dotted"] = {"pipeline.page_loader.pdf_max_pages": args.max_pages}

    started = time.perf_counter()
    layout_device = args.layout_device

    with GlmOcr(
        config_path=str(args.config),
        layout_device=layout_device,
        **overrides,
    ) as parser:
        result = parser.parse(str(args.pdf))
        result.save(output_dir=str(args.output))

    elapsed = time.perf_counter() - started

    summary = {
        "pdf": str(args.pdf.resolve()),
        "output_dir": str(args.output.resolve()),
        "config": str(args.config.resolve()),
        "layout_device": layout_device or "config/default",
        "max_pages": args.max_pages,
        "elapsed_sec": round(elapsed, 2),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    summary_path = args.output / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md_files = sorted(args.output.rglob("*.md"))
    json_files = sorted(args.output.rglob("*.json"))
    print(f"Done in {elapsed:.1f}s")
    print(f"Output: {args.output}")
    if md_files:
        print(f"Markdown: {md_files[0]}")
    if json_files:
        print(f"JSON: {json_files[0]}")
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
