#!/usr/bin/env python3
"""
GLM-OCR Parse Service — wraps the glmocr SDK as an HTTP API.

Runs in the configured OCR environment (which has torch + torchvision + glmocr).
Accepts PDF or image uploads, returns parsed Markdown text.

Start:
    python scripts/glm_ocr_service.py
    # or with custom settings:
    python scripts/glm_ocr_service.py --port 18090 --layout-device cuda:1

API:
    POST /parse
    Content-Type: multipart/form-data
    Body: file=<PDF or image file (.png .jpg .jpeg .webp .bmp .gif)>
    Optional query params: max_pages=<int> (PDF only)
    Response: {"text": "<markdown>", "chars": <int>, "elapsed_sec": <float>, "content_type": "pdf"|"image"}

    POST /parse_image
    Same as /parse but image-only (alias for clarity).

    GET /health
    Response: {"status": "ok", "model": "glm-ocr"}
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import threading
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("glm-ocr-service")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 18090
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "glm_ocr.yaml"

SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
SUPPORTED_PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = SUPPORTED_IMAGE_SUFFIXES | SUPPORTED_PDF_SUFFIXES

app = FastAPI(title="GLM-OCR Parse Service", version="1.1.0")

# Lazy-loaded parser (heavy: loads layout model + connects to vLLM)
_parser = None
_parser_config_path: Path = DEFAULT_CONFIG
_parser_layout_device: str | None = None
_parser_lock = threading.Lock()


def _get_parser():
    """Lazy-init the GlmOcr parser (first request loads layout model)."""
    global _parser
    if _parser is not None:
        return _parser

    from glmocr import GlmOcr

    config_path = Path(_parser_config_path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"GLM-OCR config not found: {config_path}")

    logger.info("Initializing GLM-OCR parser (config=%s, device=%s)...", config_path, _parser_layout_device or "default")

    _parser = GlmOcr(
        config_path=config_path,
        layout_device=_parser_layout_device,
        mode="selfhosted",
    )
    # Trigger lazy init of internal components
    logger.info("GLM-OCR parser ready.")
    return _parser


def _content_type_from_bytes(data: bytes, *, filename: str = "") -> str:
    """Detect pdf vs image from magic bytes or filename suffix."""
    if data[:5] == b"%PDF-":
        return "pdf"
    suffix = Path(filename).suffix.lower()
    if suffix in SUPPORTED_IMAGE_SUFFIXES:
        return "image"
    if suffix in SUPPORTED_PDF_SUFFIXES:
        return "pdf"
    return "image"


def _validate_filename(filename: str, *, images_only: bool = False) -> str:
    """Return normalized suffix; raise ValueError if unsupported."""
    suffix = Path(filename).suffix.lower()
    if images_only:
        if suffix not in SUPPORTED_IMAGE_SUFFIXES:
            raise ValueError(
                f"Only image files are accepted: {', '.join(sorted(SUPPORTED_IMAGE_SUFFIXES))}"
            )
        return suffix
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            "Unsupported file type. Accepted: "
            f"{', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )
    return suffix


def _parse_document_bytes(
    data: bytes,
    *,
    filename: str = "",
    max_pages: int | None = None,
) -> dict:
    """Parse PDF or image bytes using glmocr SDK, return result dict."""
    parser = _get_parser()
    content_type = _content_type_from_bytes(data, filename=filename)

    if max_pages is not None and content_type != "pdf":
        logger.warning("max_pages ignored for image input (%s)", filename or "<bytes>")

    t0 = time.time()
    with _parser_lock:
        result = parser.parse(data)
    elapsed = time.time() - t0

    text = _extract_text(result)
    return {
        "text": text,
        "chars": len(text),
        "elapsed_sec": round(elapsed, 2),
        "content_type": content_type,
    }


def _extract_text(result) -> str:
    """Extract text from glmocr SDK result."""
    for attr in ("markdown", "text", "content", "md"):
        val = getattr(result, attr, None)
        if val and isinstance(val, str) and val.strip():
            return val

    if hasattr(result, "_markdown"):
        return result._markdown or ""

    # Save to temp dir and read .md file
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            result.save(output_dir=tmpdir)
            md_files = list(Path(tmpdir).rglob("*.md"))
            if md_files:
                return md_files[0].read_text(encoding="utf-8")
        except Exception:
            logger.exception("Failed to extract Markdown from SDK result")

    return str(result)


# ── Routes ──────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok", "model": "glm-ocr", "service": "glm-ocr-parse"}


async def _handle_parse_upload(
    file: UploadFile,
    *,
    max_pages: int | None,
    images_only: bool,
) -> JSONResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    try:
        _validate_filename(file.filename, images_only=images_only)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    logger.info(
        "Parsing: %s (%d bytes, max_pages=%s, images_only=%s)",
        file.filename, len(data), max_pages, images_only,
    )

    try:
        result = await asyncio.to_thread(
            _parse_document_bytes,
            data, filename=file.filename, max_pages=max_pages,
        )
    except Exception as e:
        logger.error("Parse failed for %s: %s", file.filename, e)
        raise HTTPException(status_code=500, detail=f"Parse failed: {e}")

    logger.info(
        "Done: %s (%s) → %d chars in %.1fs",
        file.filename, result["content_type"], result["chars"], result["elapsed_sec"],
    )
    return JSONResponse(content={
        "filename": file.filename,
        "text": result["text"],
        "chars": result["chars"],
        "elapsed_sec": result["elapsed_sec"],
        "content_type": result["content_type"],
    })


@app.post("/parse")
async def parse_document(
    file: UploadFile = File(..., description="PDF or image file to parse"),
    max_pages: int | None = Query(None, description="Max pages to parse for PDF (None=all)"),
):
    return await _handle_parse_upload(file, max_pages=max_pages, images_only=False)


@app.post("/parse_image")
async def parse_image(
    file: UploadFile = File(..., description="Image file to parse"),
):
    return await _handle_parse_upload(file, max_pages=None, images_only=True)


@app.post("/parse_bytes")
async def parse_bytes(
    request: Request,
    max_pages: int | None = Query(None),
):
    """Alternative endpoint: raw PDF or image bytes in request body (no multipart)."""
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")

    logger.info("Parsing raw bytes: %d bytes, max_pages=%s", len(body), max_pages)

    try:
        result = await asyncio.to_thread(
            _parse_document_bytes, body, max_pages=max_pages,
        )
    except Exception as e:
        logger.error("Parse failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Parse failed: {e}")

    return JSONResponse(content={
        "text": result["text"],
        "chars": result["chars"],
        "elapsed_sec": result["elapsed_sec"],
        "content_type": result["content_type"],
    })


# ── Main ────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="GLM-OCR Parse Service")
    parser.add_argument(
        "--host",
        default=os.environ.get("GLM_OCR_HOST", "0.0.0.0"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("GLM_OCR_PORT", DEFAULT_PORT)),
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("GLM_OCR_CONFIG", str(DEFAULT_CONFIG)),
        help="GLM-OCR SDK config YAML (env: GLM_OCR_CONFIG)",
    )
    parser.add_argument(
        "--layout-device",
        default=os.environ.get("GLM_OCR_LAYOUT_DEVICE"),
        help="Layout model device (env: GLM_OCR_LAYOUT_DEVICE)",
    )
    args = parser.parse_args()

    global _parser_config_path, _parser_layout_device
    _parser_config_path = Path(args.config)
    _parser_layout_device = args.layout_device

    import uvicorn

    logger.info("Starting GLM-OCR Parse Service on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

