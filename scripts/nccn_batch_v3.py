#!/usr/bin/env python3
"""Batch-test NCCN PDFs against GLM-OCR v3 (figure enrichment).

Example:
  python scripts/nccn_batch_v3.py \\
    --base-url http://127.0.0.1:18094 \\
    --pdf-dir /mnt/d/de/de/pdf/NCCN \\
    --out-dir result/glm_ocr/nccn_test_v3
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18094")
    parser.add_argument("--pdf-dir", default="/mnt/d/de/de/pdf/NCCN")
    parser.add_argument("--out-dir", default="result/glm_ocr/nccn_test_v3")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--enrich-figures", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process the first N PDFs (0 = all)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pdf_dir = Path(args.pdf_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()

    health = session.get(f"{args.base_url.rstrip('/')}/health", timeout=15)
    health.raise_for_status()
    health_payload = health.json()
    print("health:", json.dumps(health_payload, ensure_ascii=False)[:500])
    service = str(health_payload.get("service", ""))
    if "v3" not in service:
        print(f"WARNING: expected glm-ocr v3 service, got {service!r}")

    pdfs = sorted(pdf_dir.glob("*.pdf"))
    names = {p.name for p in pdfs}
    pdfs = [
        p for p in pdfs
        if not (p.name == "aml_nccn.pdf" and "aml_nccn_2026.pdf" in names)
    ]
    if args.limit > 0:
        pdfs = pdfs[: args.limit]
    print(f"pdfs={len(pdfs)} enrich_figures={args.enrich_figures}")

    def parse_one(path: Path) -> dict:
        started = time.perf_counter()
        stem = path.stem
        meta = {
            "file": path.name,
            "bytes": path.stat().st_size,
            "endpoint": args.base_url,
            "enrich_figures": args.enrich_figures,
        }
        try:
            with path.open("rb") as handle:
                response = session.post(
                    f"{args.base_url.rstrip('/')}/parse",
                    params={"enrich_figures": str(args.enrich_figures).lower()},
                    files={"file": (path.name, handle, "application/pdf")},
                    timeout=args.timeout,
                )
            meta["http_status"] = response.status_code
            meta["elapsed_sec_client"] = round(time.perf_counter() - started, 2)
            if response.status_code != 200:
                meta["status"] = "failed"
                meta["error"] = response.text[:2000]
            else:
                data = response.json()
                text = data.get("text") or ""
                meta.update({
                    "status": "completed",
                    "job_id": data.get("job_id"),
                    "chars": data.get("chars", len(text)),
                    "elapsed_sec_server": data.get("elapsed_sec"),
                    "figure_enrichment": data.get("figure_enrichment"),
                    "has_mermaid": "```mermaid" in text.lower(),
                    "has_image_caption": "**image caption:**" in text.lower(),
                    "markdown_image_refs": text.count("!["),
                })
                (out_dir / f"{stem}.md").write_text(text, encoding="utf-8")
                slim = {k: v for k, v in data.items() if k != "text"}
                slim["meta"] = meta
                slim["text_preview"] = text[:500]
                (out_dir / f"{stem}.json").write_text(
                    json.dumps(slim, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(
                    f"OK {path.name} chars={meta['chars']} "
                    f"client={meta['elapsed_sec_client']}s "
                    f"figures={meta.get('figure_enrichment')}"
                )
                return meta
        except Exception as exc:  # noqa: BLE001
            meta["status"] = "failed"
            meta["error"] = f"{type(exc).__name__}: {exc}"
            meta["elapsed_sec_client"] = round(time.perf_counter() - started, 2)
        (out_dir / f"{stem}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"FAIL {path.name}: {meta.get('error')}")
        return meta

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(parse_one, path) for path in pdfs]
        for future in as_completed(futures):
            results.append(future.result())

    summary = {
        "endpoint": args.base_url,
        "total": len(results),
        "completed": sum(1 for row in results if row.get("status") == "completed"),
        "failed": sum(1 for row in results if row.get("status") != "completed"),
        "results": sorted(results, key=lambda row: row.get("file", "")),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "total": summary["total"],
        "completed": summary["completed"],
        "failed": summary["failed"],
    }, ensure_ascii=False))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
