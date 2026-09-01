#!/usr/bin/env python3
"""Poll one glm-ocr-v3 gateway task and save the result."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

BASE = "http://172.19.64.72/glm-ocr-v3"
JOB = sys.argv[1] if len(sys.argv) > 1 else "castleman-disease-nccn-2026_20260901183656"
OUT = Path("/home/wilson/data/glm_ocr/result/glm_ocr/nccn_test_gateway_v3")
OUT.mkdir(parents=True, exist_ok=True)


def main() -> int:
    session = requests.Session()
    status_payload: dict = {}
    for index in range(180):
        response = session.get(f"{BASE}/tasks/{JOB}/status", timeout=30)
        response.raise_for_status()
        status_payload = response.json()
        status = status_payload.get("status")
        print(
            f"[{index}] {status} chars={status_payload.get('chars')} "
            f"err={status_payload.get('error')}"
        )
        if status in {"completed", "failed", "canceled"}:
            break
        time.sleep(3)
    else:
        print("timeout waiting for job", file=sys.stderr)
        return 1

    (OUT / f"{JOB}.status.json").write_text(
        json.dumps(status_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if status_payload.get("status") != "completed":
        print(f"job ended as {status_payload.get('status')}", file=sys.stderr)
        return 1

    result = session.get(f"{BASE}/tasks/{JOB}/result", timeout=120)
    result.raise_for_status()
    data = result.json()
    text = data.get("text") or ""
    (OUT / f"{JOB}.md").write_text(text, encoding="utf-8")
    slim = {key: value for key, value in data.items() if key != "text"}
    slim["text_preview"] = text[:500]
    slim["has_mermaid"] = "```mermaid" in text.lower()
    slim["has_image_caption"] = "image caption" in text.lower()
    slim["markdown_image_refs"] = text.count("![")
    slim["figure_enrichment"] = data.get("figure_enrichment")
    (OUT / f"{JOB}.json").write_text(
        json.dumps(slim, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "chars",
        len(text),
        "mermaid",
        slim["has_mermaid"],
        "caption",
        slim["has_image_caption"],
        "figure_enrichment",
        slim["figure_enrichment"],
    )
    print("keys", sorted(data)[:40])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
