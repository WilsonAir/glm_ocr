#!/usr/bin/env python3
"""Submit an OSS object to GLM-OCR v5 and poll the asynchronous result."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://172.19.64.72/glm-ocr-v5",
        help="Gateway base URL, without /parse_oss",
    )
    parser.add_argument(
        "--oss-path",
        required=True,
        help="Object key inside the server-side OSS bucket, e.g. docs/test.pdf",
    )
    parser.add_argument(
        "--job-id",
        default=None,
        help="Optional stable job ID; generated when omitted",
    )
    parser.add_argument(
        "--translate",
        action="store_true",
        help="Enable local LLM translation for layout jobs",
    )
    parser.add_argument(
        "--ocr-engine",
        choices=("glm", "paddle"),
        default=None,
        help="Optional OCR backend override",
    )
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    job_id = args.job_id or (
        "oss-test-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    payload = {
        "job_id": job_id,
        "oss_path": args.oss_path,
        "image_mode": "auto",
        "enrich_figures": False,
        "translate": args.translate,
        "translate_provider": "llm",
        "attempt": 1,
    }
    if args.ocr_engine:
        payload["ocr_engine"] = args.ocr_engine

    submit_url = f"{base_url}/parse_oss"
    result_url = f"{base_url}/results/{job_id}"
    print(f"POST {submit_url}")
    print(f"job_id={job_id} oss_path={args.oss_path}")
    try:
        response = requests.post(submit_url, json=payload, timeout=30)
    except requests.RequestException as exc:
        print(f"submit request failed: {exc}", file=sys.stderr)
        return 2
    if response.status_code != 202:
        print(f"submit failed: HTTP {response.status_code}", file=sys.stderr)
        try:
            print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        except ValueError:
            print(response.text, file=sys.stderr)
        return 3
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))

    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = requests.get(result_url, timeout=30)
        except requests.RequestException as exc:
            print(f"poll request failed: {exc}; retrying", file=sys.stderr)
            time.sleep(args.poll_seconds)
            continue
        if response.status_code == 404:
            print("result metadata not ready; retrying")
            time.sleep(args.poll_seconds)
            continue
        if response.status_code != 200:
            print(f"poll failed: HTTP {response.status_code}", file=sys.stderr)
            print(response.text, file=sys.stderr)
            return 4

        result = response.json()
        status = result.get("status")
        print(f"status={status} queue={result.get('queue')}")
        if status == "completed":
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"result_url={result_url}")
            return 0
        if status in {"failed", "rejected"}:
            print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
            return 5
        time.sleep(args.poll_seconds)

    print(f"timed out after {args.timeout_seconds}s; poll {result_url}", file=sys.stderr)
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
