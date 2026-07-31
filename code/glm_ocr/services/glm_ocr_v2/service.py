#!/usr/bin/env python3
"""GLM-OCR HTTP service that persists the complete official SDK result."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("glm-ocr-service-v2")

GLM_OCR_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG = GLM_OCR_ROOT / "config" / "glm_ocr.yaml"
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT / "result" / "glm_ocr" / "framework"
DEFAULT_PORT = 18091

SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
SUPPORTED_PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = SUPPORTED_IMAGE_SUFFIXES | SUPPORTED_PDF_SUFFIXES

app = FastAPI(title="GLM-OCR Persistent Parse Service", version="2.0.0")

_parser: Any = None
_parser_config_path = DEFAULT_CONFIG
_parser_layout_device: str | None = None
_output_root = DEFAULT_OUTPUT_ROOT
_parser_init_lock = threading.Lock()
_parse_lock = threading.Lock()


def _get_parser() -> Any:
    """Lazily construct the shared official SDK parser exactly once."""
    global _parser
    if _parser is not None:
        return _parser

    with _parser_init_lock:
        if _parser is not None:
            return _parser

        from glmocr import GlmOcr

        config_path = _parser_config_path.expanduser().resolve()
        if not config_path.is_file():
            raise FileNotFoundError(f"GLM-OCR config not found: {config_path}")

        logger.info(
            "Initializing GLM-OCR parser (config=%s, device=%s)",
            config_path,
            _parser_layout_device or "default",
        )
        _parser = GlmOcr(
            config_path=str(config_path),
            layout_device=_parser_layout_device,
            mode="selfhosted",
        )
        logger.info("GLM-OCR parser ready")
        return _parser


def _validate_filename(filename: str, *, images_only: bool = False) -> str:
    suffix = Path(filename).suffix.lower()
    allowed = SUPPORTED_IMAGE_SUFFIXES if images_only else SUPPORTED_SUFFIXES
    if suffix not in allowed:
        accepted = ", ".join(sorted(allowed))
        raise ValueError(f"Unsupported file type. Accepted: {accepted}")
    return suffix


def _safe_job_dir(job_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(job_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid job UUID") from exc
    return _output_root.resolve() / normalized


def _relative_files(job_dir: Path) -> list[str]:
    return sorted(
        str(path.relative_to(job_dir))
        for path in job_dir.rglob("*")
        if path.is_file()
    )


def _extract_markdown(result: Any) -> str:
    for attr in ("markdown_result", "markdown", "text", "content", "md"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _write_metadata(job_dir: Path, metadata: dict[str, Any]) -> None:
    (job_dir / "job.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _parse_and_save(
    data: bytes,
    *,
    filename: str,
    job_id: str,
    content_type: str,
) -> dict[str, Any]:
    """Run layout/OCR and save every artifact exposed by the official SDK."""
    job_dir = _safe_job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=False)
    started = time.time()

    initial_metadata: dict[str, Any] = {
        "job_id": job_id,
        "status": "processing",
        "filename": filename,
        "content_type": content_type,
        "created_at": started,
        "output_dir": str(job_dir),
    }
    _write_metadata(job_dir, initial_metadata)

    try:
        parser = _get_parser()
        with _parse_lock:
            result = parser.parse(data)
            # Official SDK persists JSON/Markdown, image crops under imgs/, and
            # layout visualizations under layout_vis/ when those are available.
            result.save(output_dir=str(job_dir))

        markdown = _extract_markdown(result)
        elapsed = round(time.time() - started, 2)
        metadata = {
            **initial_metadata,
            "status": "completed",
            "chars": len(markdown),
            "elapsed_sec": elapsed,
        }
        _write_metadata(job_dir, metadata)
        files = _relative_files(job_dir)
        return {
            **metadata,
            "text": markdown,
            "artifacts": files,
            "result_url": f"/results/{job_id}",
        }
    except Exception as exc:
        elapsed = round(time.time() - started, 2)
        metadata = {
            **initial_metadata,
            "status": "failed",
            "elapsed_sec": elapsed,
            "error": str(exc),
        }
        _write_metadata(job_dir, metadata)
        logger.exception("Parse failed for %s (job=%s)", filename, job_id)
        raise


async def _handle_upload(
    file: UploadFile,
    *,
    images_only: bool,
) -> JSONResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    try:
        suffix = _validate_filename(file.filename, images_only=images_only)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    job_id = str(uuid.uuid4())
    content_type = "pdf" if suffix in SUPPORTED_PDF_SUFFIXES else "image"
    logger.info(
        "Parsing %s (%d bytes, job=%s)",
        file.filename,
        len(data),
        job_id,
    )
    try:
        response = await asyncio.to_thread(
            _parse_and_save,
            data,
            filename=file.filename,
            job_id=job_id,
            content_type=content_type,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"message": "Parse failed", "job_id": job_id, "error": str(exc)},
        ) from exc
    return JSONResponse(response)


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "model": "glm-ocr",
        "service": "glm-ocr-persistent-parse-v2",
        "output_root": str(_output_root.resolve()),
    }


@app.post("/parse")
async def parse_document(
    file: UploadFile = File(..., description="PDF or image file to parse"),
) -> JSONResponse:
    return await _handle_upload(file, images_only=False)


@app.post("/parse_image")
async def parse_image(
    file: UploadFile = File(..., description="Image file to parse"),
) -> JSONResponse:
    return await _handle_upload(file, images_only=True)


@app.get("/results/{job_id}")
async def get_result(job_id: str) -> JSONResponse:
    job_dir = _safe_job_dir(job_id)
    metadata_file = job_dir / "job.json"
    if not metadata_file.is_file():
        raise HTTPException(status_code=404, detail="Result not found")
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    metadata["artifacts"] = _relative_files(job_dir)
    return JSONResponse(metadata)


@app.get("/results/{job_id}/{artifact_path:path}")
async def download_artifact(job_id: str, artifact_path: str) -> FileResponse:
    job_dir = _safe_job_dir(job_id)
    requested = (job_dir / artifact_path).resolve()
    if not requested.is_relative_to(job_dir) or not requested.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(requested, filename=requested.name)


def main() -> None:
    arg_parser = argparse.ArgumentParser(
        description="Persistent GLM-OCR Parse Service v2"
    )
    arg_parser.add_argument(
        "--host",
        default=os.environ.get("GLM_OCR_V2_HOST", "127.0.0.1"),
    )
    arg_parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("GLM_OCR_V2_PORT", DEFAULT_PORT)),
    )
    arg_parser.add_argument(
        "--config",
        default=os.environ.get("GLM_OCR_V2_CONFIG", str(DEFAULT_CONFIG)),
    )
    arg_parser.add_argument(
        "--layout-device",
        default=os.environ.get("GLM_OCR_V2_LAYOUT_DEVICE", "cuda:1"),
    )
    arg_parser.add_argument(
        "--output-root",
        default=os.environ.get(
            "GLM_OCR_V2_OUTPUT_ROOT",
            str(DEFAULT_OUTPUT_ROOT),
        ),
    )
    args = arg_parser.parse_args()

    global _parser_config_path, _parser_layout_device, _output_root
    _parser_config_path = Path(args.config)
    _parser_layout_device = args.layout_device
    _output_root = Path(args.output_root).expanduser().resolve()
    _output_root.mkdir(parents=True, exist_ok=True)

    import uvicorn

    logger.info(
        "Starting GLM-OCR v2 on %s:%d (output=%s)",
        args.host,
        args.port,
        _output_root,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
