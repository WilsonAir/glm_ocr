#!/usr/bin/env python3
"""PaddleOCR-VL framework inference (doc_parser pipeline + vLLM server)."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_PDF = Path("/data/wilson_2/de/medical_paper_catalog/sample_pdfs/aml_nccn.pdf")
DEFAULT_OUTPUT = PROJECT_ROOT / "result" / "framework"
DEFAULT_LAYOUT_MODEL_DIR = Path(
    "/data/wilson_2/.paddlex/official_models/PP-DocLayoutV3"
)
DEFAULT_VLLM_URL = "http://127.0.0.1:18081/v1"
DEFAULT_VLLM_MODEL = "paddleocr-vl-1.6"
DEFAULT_VLLM_MAX_CONCURRENCY = 8


def merge_page_markdown(output_dir: Path, pdf_stem: str) -> Path:
    """Merge per-page SDK markdown into one file (PaddleX saves {stem}_{page}.md per page)."""
    page_files = sorted(
        output_dir.glob(f"{pdf_stem}_*.md"),
        key=lambda p: int(p.stem.rsplit("_", 1)[-1]),
    )
    if not page_files:
        return output_dir / f"{pdf_stem}.md"

    merged_path = output_dir / f"{pdf_stem}.md"
    parts = [
        f"# PaddleOCR-VL Framework: {pdf_stem}.pdf",
        "",
        f"- Pages: {len(page_files)}",
        f"- Per-page files: `{pdf_stem}_<page>.md`",
        "",
    ]
    for page_file in page_files:
        page_num = int(page_file.stem.rsplit("_", 1)[-1]) + 1
        parts.extend([f"## Page {page_num}", "", page_file.read_text(encoding="utf-8").strip(), ""])
    merged_path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    return merged_path

def main() -> None:
    parser = argparse.ArgumentParser(description="PaddleOCR-VL doc_parser pipeline test")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--vllm-url", default=DEFAULT_VLLM_URL)
    parser.add_argument(
        "--vllm-model",
        default=DEFAULT_VLLM_MODEL,
        help="vLLM served model name (must match --served-model-name in serve_paddleocr_vl.sh)",
    )
    parser.add_argument(
        "--vllm-max-concurrency",
        type=int,
        default=DEFAULT_VLLM_MAX_CONCURRENCY,
        help="Max parallel vLLM requests (paddlex default 200 can crash vLLM)",
    )
    parser.add_argument(
        "--layout-model-dir",
        type=Path,
        default=DEFAULT_LAYOUT_MODEL_DIR,
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Paddle layout device; cpu required on PPU 2.0 (cudnn init fails on gpu). VL via vLLM stays on PPU.",
    )
    args = parser.parse_args()

    if not args.pdf.is_file():
        raise SystemExit(f"PDF not found: {args.pdf}")

    try:
        from paddleocr import PaddleOCRVL
    except ImportError as exc:
        raise SystemExit(
            "paddleocr not installed. Run: bash setup_env.sh\n" + str(exc)
        ) from exc

    args.output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    pipeline = PaddleOCRVL(
        pipeline_version="v1.6",
        vl_rec_backend="vllm-server",
        vl_rec_server_url=args.vllm_url,
        vl_rec_api_model_name=args.vllm_model,
        vl_rec_max_concurrency=args.vllm_max_concurrency,
        layout_detection_model_dir=str(args.layout_model_dir),
        device=args.device,
        use_queues=False,
    )
    outputs = pipeline.predict(str(args.pdf))
    for res in outputs:
        res.save_to_json(save_path=str(args.output))
        res.save_to_markdown(save_path=str(args.output))

    merged_md = merge_page_markdown(args.output, args.pdf.stem)

    elapsed = time.perf_counter() - started
    summary = {
        "mode": "framework_doc_parser",
        "note": "layout on paddle cpu (PPU cudnn issue); VL recognition via vLLM on PPU gpu",
        "pdf": str(args.pdf.resolve()),
        "vllm_url": args.vllm_url,
        "vllm_model": args.vllm_model,
        "vllm_max_concurrency": args.vllm_max_concurrency,
        "device": args.device,
        "layout_model_dir": str(args.layout_model_dir),
        "output_dir": str(args.output.resolve()),
        "markdown_merged": str(merged_md.resolve()),
        "page_markdown_pattern": f"{args.pdf.stem}_<page>.md",
        "elapsed_sec": round(elapsed, 2),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Done in {elapsed:.1f}s -> {args.output}")


if __name__ == "__main__":
    main()
