#!/usr/bin/env python3
"""PaddleOCR-VL framework inference (official doc_parser + vLLM server)."""

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
DEFAULT_VLLM_MODEL = "PaddleOCR-VL-1.6"
DEFAULT_VLLM_MAX_CONCURRENCY = 8


def extract_markdown(pipeline, pages_res) -> str:
    markdown_list = []
    for res in pages_res:
        md = getattr(res, "markdown", None)
        if isinstance(md, dict):
            markdown_list.append(md)
    if not markdown_list:
        return ""
    text = pipeline.concatenate_markdown_pages(markdown_list)
    if isinstance(text, tuple):
        text = text[0] if text else ""
    return str(text).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="PaddleOCR-VL official pipeline test")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--vllm-url", default=DEFAULT_VLLM_URL)
    parser.add_argument(
        "--vllm-model",
        default=DEFAULT_VLLM_MODEL,
        help="vLLM served model name (must match /v1/models id)",
    )
    parser.add_argument(
        "--vllm-max-concurrency",
        type=int,
        default=DEFAULT_VLLM_MAX_CONCURRENCY,
    )
    parser.add_argument(
        "--layout-model-dir",
        type=Path,
        default=DEFAULT_LAYOUT_MODEL_DIR,
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Layout device; cpu required on PPU 2.0 (cudnn init fails on gpu).",
    )
    args = parser.parse_args()

    if not args.pdf.is_file():
        raise SystemExit(f"PDF not found: {args.pdf}")

    try:
        from paddleocr import PaddleOCRVL
    except ImportError as exc:
        raise SystemExit(
            "paddleocr not installed. Activate conda env paddle_ppu.\n" + str(exc)
        ) from exc

    args.output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    # Official Python API:
    #   PaddleOCRVL(vl_rec_backend="vllm-server", vl_rec_server_url=...)
    #   predict() -> restructure_pages() -> save_to_json / save_to_markdown
    pipeline_kwargs = {
        "pipeline_version": "v1.6",
        "vl_rec_backend": "vllm-server",
        "vl_rec_server_url": args.vllm_url,
        "vl_rec_api_model_name": args.vllm_model,
        "vl_rec_max_concurrency": args.vllm_max_concurrency,
        "use_layout_detection": True,
        "layout_detection_model_name": "PP-DocLayoutV3",
        "device": args.device,
    }
    if args.layout_model_dir.is_dir():
        pipeline_kwargs["layout_detection_model_dir"] = str(args.layout_model_dir)

    pipeline = PaddleOCRVL(**pipeline_kwargs)
    pages_res = list(
        pipeline.predict(input=str(args.pdf), use_layout_detection=True)
    )
    if len(pages_res) > 1:
        pages_res = list(
            pipeline.restructure_pages(
                pages_res,
                merge_tables=True,
                relevel_titles=True,
                concatenate_pages=True,
            )
        )
    for res in pages_res:
        res.save_to_json(save_path=str(args.output))
        res.save_to_markdown(save_path=str(args.output))

    markdown = extract_markdown(pipeline, pages_res)
    merged_md = args.output / f"{args.pdf.stem}.md"
    if markdown:
        merged_md.write_text(markdown.rstrip() + "\n", encoding="utf-8")

    elapsed = time.perf_counter() - started
    summary = {
        "mode": "official_paddleocr_vl_pipeline",
        "note": "layout=PP-DocLayoutV3; VL via vLLM; PDF via restructure_pages",
        "pdf": str(args.pdf.resolve()),
        "vllm_url": args.vllm_url,
        "vllm_model": args.vllm_model,
        "vllm_max_concurrency": args.vllm_max_concurrency,
        "device": args.device,
        "layout_model_dir": str(args.layout_model_dir),
        "output_dir": str(args.output.resolve()),
        "markdown_merged": str(merged_md.resolve()),
        "elapsed_sec": round(elapsed, 2),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Done in {elapsed:.1f}s -> {args.output}")


if __name__ == "__main__":
    main()
