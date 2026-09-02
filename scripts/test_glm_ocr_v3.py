#!/usr/bin/env python3
"""GLM-OCR v3 smoke / Mermaid enrichment test.

On train2 (service on :18094):

  python scripts/test_glm_ocr_v3.py \\
    --base-url http://127.0.0.1:18094 \\
    --pdf /data/wilson_2/de/pdf/NCCN/castleman-disease-nccn-2026.pdf

Via gateway:

  python scripts/test_glm_ocr_v3.py \\
    --base-url http://172.19.64.72/glm-ocr-v3 \\
    --pdf /mnt/d/de/de/pdf/NCCN/castleman-disease-nccn-2026.pdf

Batch a few NCCN PDFs:

  python scripts/test_glm_ocr_v3.py \\
    --base-url http://127.0.0.1:18094 \\
    --pdf-dir /data/wilson_2/de/pdf/NCCN \\
    --limit 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:18094",
        help="v3 service root (no trailing slash required)",
    )
    parser.add_argument("--pdf", type=Path, help="Single PDF to parse")
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        help="Directory of PDFs (used when --pdf is omitted)",
    )
    parser.add_argument("--limit", type=int, default=1, help="Max PDFs from --pdf-dir")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("result/glm_ocr/v3_smoke"),
    )
    parser.add_argument("--enrich-figures", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--timeout", type=int, default=1800)
    return parser.parse_args()


def analyze_text(text: str) -> dict:
    lower = text.lower()
    return {
        "chars": len(text),
        "mermaid_fences": lower.count("```mermaid"),
        "markdown_fences": lower.count("```markdown"),
        "image_captions": lower.count("image caption"),
        "image_refs": text.count("!["),
        "flowchart_kw": lower.count("flowchart"),
        "subgraph_kw": lower.count("subgraph"),
    }


def health_check(session: requests.Session, base: str) -> dict:
    url = f"{base}/health"
    response = session.get(url, timeout=15)
    response.raise_for_status()
    payload = response.json()
    print("== health ==")
    print(json.dumps({
        "status": payload.get("status"),
        "service": payload.get("service"),
        "figure_enrichment": payload.get("figure_enrichment"),
    }, ensure_ascii=False, indent=2))
    if "v3" not in str(payload.get("service", "")):
        raise RuntimeError(f"Not a v3 service: {payload.get('service')!r}")
    return payload


def parse_pdf(
    session: requests.Session,
    *,
    base: str,
    pdf: Path,
    out_dir: Path,
    enrich_figures: bool,
    timeout: int,
) -> dict:
    if not pdf.is_file():
        raise FileNotFoundError(pdf)
    started = time.perf_counter()
    print(f"\n== parse {pdf.name} ({pdf.stat().st_size} bytes) ==")
    with pdf.open("rb") as handle:
        response = session.post(
            f"{base}/parse",
            params={"enrich_figures": str(enrich_figures).lower()},
            files={"file": (pdf.name, handle, "application/pdf")},
            timeout=timeout,
        )
    elapsed = round(time.perf_counter() - started, 2)
    if response.status_code != 200:
        meta = {
            "file": pdf.name,
            "status": "failed",
            "http_status": response.status_code,
            "elapsed_sec": elapsed,
            "error": response.text[:2000],
        }
        print("FAIL", meta["error"][:300])
        return meta

    data = response.json()
    text = data.get("text") or ""
    stats = analyze_text(text)
    fe = data.get("figure_enrichment") or {}
    kinds: dict[str, int] = {}
    for item in fe.get("converted") or []:
        kinds[item.get("kind", "?")] = kinds.get(item.get("kind", "?"), 0) + 1

    meta = {
        "file": pdf.name,
        "status": data.get("status", "completed"),
        "job_id": data.get("job_id"),
        "http_status": response.status_code,
        "elapsed_sec_client": elapsed,
        "elapsed_sec_server": data.get("elapsed_sec"),
        "chars": data.get("chars", stats["chars"]),
        "enrich_figures": data.get("enrich_figures"),
        "figure_image_count": fe.get("image_count"),
        "figure_kinds": kinds,
        "figure_errors": fe.get("errors"),
        "text_stats": stats,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf.stem
    (out_dir / f"{stem}.md").write_text(text, encoding="utf-8")
    slim = {k: v for k, v in data.items() if k != "text"}
    slim["meta"] = meta
    slim["text_preview"] = text[:800]
    (out_dir / f"{stem}.json").write_text(
        json.dumps(slim, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"OK job={meta.get('job_id')} chars={meta['chars']} "
        f"client={elapsed}s server={meta.get('elapsed_sec_server')} "
        f"imgs={meta.get('figure_image_count')} kinds={kinds}"
    )
    print(
        f"   mermaid={stats['mermaid_fences']} markdown_fence={stats['markdown_fences']} "
        f"caption={stats['image_captions']} subgraph={stats['subgraph_kw']}"
    )
    return meta


def collect_pdfs(args: argparse.Namespace) -> list[Path]:
    if args.pdf:
        return [args.pdf]
    pdf_dir = args.pdf_dir
    if pdf_dir is None:
        candidates = [
            Path("/data/wilson_2/de/pdf/NCCN"),
            Path("/mnt/d/de/de/pdf/NCCN"),
            Path("/data/wilson_2/de/glm_ocr/sample"),
        ]
        for candidate in candidates:
            if candidate.is_dir() and any(candidate.glob("*.pdf")):
                pdf_dir = candidate
                break
    if pdf_dir is None or not pdf_dir.is_dir():
        raise SystemExit(
            "No PDF given. Pass --pdf FILE or --pdf-dir DIR "
            "(tried /data/wilson_2/de/pdf/NCCN and /mnt/d/de/de/pdf/NCCN)"
        )
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    names = {p.name for p in pdfs}
    pdfs = [
        p for p in pdfs
        if not (p.name == "aml_nccn.pdf" and "aml_nccn_2026.pdf" in names)
    ]
    # Prefer a small flowchart-heavy sample first when limit is small.
    preferred = [
        "castleman-disease-nccn-2026.pdf",
        "hairy-cell-leukemia-nccn-2027.pdf",
        "aml_nccn_2026.pdf",
    ]
    ordered = [pdf_dir / name for name in preferred if (pdf_dir / name).is_file()]
    rest = [p for p in pdfs if p not in ordered]
    pdfs = ordered + rest
    return pdfs[: max(1, args.limit)]


def main() -> int:
    args = parse_args()
    base = args.base_url.rstrip("/")
    session = requests.Session()
    try:
        health_check(session, base)
    except Exception as exc:  # noqa: BLE001
        print(f"health failed: {exc}", file=sys.stderr)
        return 2

    pdfs = collect_pdfs(args)
    print(f"\nfiles={len(pdfs)} enrich_figures={args.enrich_figures} out={args.out_dir}")
    results = []
    for pdf in pdfs:
        try:
            results.append(
                parse_pdf(
                    session,
                    base=base,
                    pdf=pdf,
                    out_dir=args.out_dir,
                    enrich_figures=args.enrich_figures,
                    timeout=args.timeout,
                )
            )
        except Exception as exc:  # noqa: BLE001
            results.append({
                "file": pdf.name,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"FAIL {pdf.name}: {exc}")

    summary = {
        "base_url": base,
        "total": len(results),
        "completed": sum(1 for row in results if row.get("status") == "completed"),
        "failed": sum(1 for row in results if row.get("status") != "completed"),
        "mermaid_total": sum(
            (row.get("text_stats") or {}).get("mermaid_fences", 0) for row in results
        ),
        "results": results,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\n== summary ==")
    print(json.dumps({
        "total": summary["total"],
        "completed": summary["completed"],
        "failed": summary["failed"],
        "mermaid_total": summary["mermaid_total"],
        "out_dir": str(args.out_dir),
    }, ensure_ascii=False, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
