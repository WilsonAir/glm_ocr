#!/usr/bin/env python3
"""GLM-OCR v2 service with separate layout and model-only queues."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import requests
import yaml
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("glm-ocr-service-v2")

GLM_OCR_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG = GLM_OCR_ROOT / "config" / "glm_ocr.yaml"
DEFAULT_SERVICE_CONFIG = GLM_OCR_ROOT / "config" / "ocr_services_v2.yaml"
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT / "result" / "glm_ocr" / "framework"
DEFAULT_PORT = 18091

SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
SUPPORTED_PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = SUPPORTED_IMAGE_SUFFIXES | SUPPORTED_PDF_SUFFIXES
MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
EXTERNAL_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$")
CALLBACK_MAX_ATTEMPTS = 3
CALLBACK_RETRY_DELAY_SEC = 1.0

_layout_parsers: dict[str, Any] = {}
_parser_config_path = DEFAULT_CONFIG
_parser_layout_device: str | None = None
_output_root = DEFAULT_OUTPUT_ROOT
_parser_init_lock = threading.Lock()

_pdf_queue_capacity = 4
_image_layout_queue_capacity = 8
_model_queue_capacity = 8
_pdf_workers = 2
_image_layout_workers = 1
_model_workers = 4
_vllm_url = "http://127.0.0.1:18080/v1/chat/completions"
_vllm_model = "glm-ocr"
_vllm_timeout = 300
_vllm_max_tokens = 4096

_oss_endpoint = ""
_oss_access_key_id = ""
_oss_access_key_secret = ""
_oss_bucket_name = ""
_oss_prefix = "glm_ocr_output"
_oss_signed_url_expires = 3600
_oss_bucket: Any = None
_callback_url = ""

_pdf_running = 0
_image_layout_running = 0
_model_running = 0

_pdf_queue: asyncio.Queue[QueueJob] | None = None
_image_layout_queue: asyncio.Queue[QueueJob] | None = None
_model_queue: asyncio.Queue[QueueJob] | None = None


@dataclass(slots=True)
class QueueJob:
    job_id: str
    filename: str
    content_type: Literal["pdf", "image"]
    mode: Literal["layout", "model_only"]
    queue_name: str
    data: bytes | None = None
    oss_path: str | None = None
    attempt: int = 1
    image_mode: Literal["auto", "layout", "model_only"] = "auto"
    future: asyncio.Future[dict[str, Any]] | None = None


def _get_parser(parser_key: str) -> Any:
    """Lazily construct one official SDK parser for each layout worker."""
    parser = _layout_parsers.get(parser_key)
    if parser is not None:
        return parser

    with _parser_init_lock:
        parser = _layout_parsers.get(parser_key)
        if parser is not None:
            return parser

        from glmocr import GlmOcr

        config_path = _parser_config_path.expanduser().resolve()
        if not config_path.is_file():
            raise FileNotFoundError(f"GLM-OCR config not found: {config_path}")

        logger.info(
            "Initializing GLM-OCR parser (config=%s, device=%s)",
            config_path,
            _parser_layout_device or "default",
        )
        parser = GlmOcr(
            config_path=str(config_path),
            layout_device=_parser_layout_device,
            mode="selfhosted",
        )
        _layout_parsers[parser_key] = parser
        logger.info("GLM-OCR layout parser ready: %s", parser_key)
        return parser


def _detect_content_type(data: bytes, filename: str) -> tuple[str, str]:
    """Validate content using magic bytes first and return type plus suffix."""
    suffix = Path(filename).suffix.lower()
    if data.startswith(b"%PDF-"):
        return "pdf", ".pdf"
    if suffix not in SUPPORTED_IMAGE_SUFFIXES:
        accepted = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ValueError(f"Unsupported file type. Accepted: {accepted}")
    return "image", suffix


def _infer_content_type_from_path(path: str) -> Literal["pdf", "image"]:
    """Select queue from object key suffix before downloading the file."""
    suffix = Path(path).suffix.lower()
    if suffix in SUPPORTED_PDF_SUFFIXES:
        return "pdf"
    if suffix in SUPPORTED_IMAGE_SUFFIXES:
        return "image"
    accepted = ", ".join(sorted(SUPPORTED_SUFFIXES))
    raise ValueError(f"Unsupported file type. Accepted: {accepted}")


def _safe_job_dir(job_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(job_id))
    except ValueError:
        if (
            not EXTERNAL_JOB_ID_RE.fullmatch(job_id)
            or ".." in job_id
            or "/" in job_id
            or "\\" in job_id
        ):
            raise HTTPException(status_code=400, detail="Invalid job id")
        normalized = job_id
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


def _beijing_now() -> str:
    """Return an ISO 8601 timestamp in Beijing time."""
    return datetime.now(BEIJING_TIMEZONE).isoformat(timespec="seconds")


def _read_metadata(job_dir: Path) -> dict[str, Any]:
    path = job_dir / "job.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_metadata(job_dir: Path, metadata: dict[str, Any]) -> None:
    (job_dir / "job.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _update_metadata(job_dir: Path, **fields: Any) -> dict[str, Any]:
    metadata = _read_metadata(job_dir)
    metadata.update(fields)
    _write_metadata(job_dir, metadata)
    return metadata


def _complete_response(
    job_dir: Path,
    metadata: dict[str, Any],
    markdown: str,
) -> dict[str, Any]:
    return {
        **metadata,
        "text": markdown,
        "artifacts": _relative_files(job_dir),
        "result_url": f"/results/{metadata['job_id']}",
    }


def _running_count(queue_name: str) -> int:
    if queue_name == "pdf_layout":
        return _pdf_running
    if queue_name == "image_layout":
        return _image_layout_running
    if queue_name == "model_only":
        return _model_running
    return 0


def _adjust_running(queue_name: str, delta: int) -> None:
    global _pdf_running, _image_layout_running, _model_running
    if queue_name == "pdf_layout":
        _pdf_running = max(0, _pdf_running + delta)
    elif queue_name == "image_layout":
        _image_layout_running = max(0, _image_layout_running + delta)
    elif queue_name == "model_only":
        _model_running = max(0, _model_running + delta)


def _oss_configured() -> bool:
    return bool(
        _oss_endpoint
        and _oss_access_key_id
        and _oss_access_key_secret
        and _oss_bucket_name
    )


def _init_oss_bucket() -> Any:
    global _oss_bucket
    if _oss_bucket is not None:
        return _oss_bucket
    if not _oss_configured():
        raise RuntimeError("OSS configuration is incomplete")
    try:
        import oss2
    except ImportError as exc:
        raise RuntimeError("OSS support requires 'oss2' package") from exc
    auth = oss2.Auth(_oss_access_key_id, _oss_access_key_secret)
    _oss_bucket = oss2.Bucket(auth, _oss_endpoint, _oss_bucket_name)
    return _oss_bucket


def _download_from_oss(oss_path: str) -> tuple[bytes, str]:
    bucket = _init_oss_bucket()
    object_key = oss_path.lstrip("/")
    try:
        result = bucket.get_object(object_key)
        data = result.read()
    except Exception as exc:
        raise RuntimeError(f"Failed to download from OSS: {object_key}") from exc
    filename = Path(object_key).name
    return data, filename


def _upload_to_oss(job_dir: Path, *, oss_output_prefix: str) -> list[dict[str, str]]:
    bucket = _init_oss_bucket()
    base_key = oss_output_prefix.strip("/")
    uploaded: list[dict[str, str]] = []
    for file_path in job_dir.rglob("*"):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(job_dir)
        object_key = f"{base_key}/{relative.as_posix()}"
        content_type = "application/octet-stream"
        if file_path.suffix == ".md":
            content_type = "text/markdown; charset=utf-8"
        elif file_path.suffix == ".json":
            content_type = "application/json; charset=utf-8"
        elif file_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
            content_type = f"image/{file_path.suffix.lstrip('.').lower()}"
        bucket.put_object_from_file(
            object_key,
            str(file_path),
            headers={"Content-Type": content_type},
        )
        url = bucket.sign_url("GET", object_key, _oss_signed_url_expires)
        uploaded.append({
            "path": relative.as_posix(),
            "oss_key": object_key,
            "url": url,
        })
    return uploaded


def _post_callback(payload: dict[str, Any]) -> None:
    if not _callback_url:
        logger.warning(
            "GLM_OCR_V2_CALLBACK_URL is not set; skip callback for job=%s",
            payload.get("job_id"),
        )
        return
    last_error: Exception | None = None
    for attempt in range(1, CALLBACK_MAX_ATTEMPTS + 1):
        try:
            response = requests.post(_callback_url, json=payload, timeout=30)
            response.raise_for_status()
            logger.info(
                "Callback succeeded for job=%s attempt=%s",
                payload.get("job_id"),
                attempt,
            )
            return
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Callback failed for job=%s (try %d/%d): %s",
                payload.get("job_id"),
                attempt,
                CALLBACK_MAX_ATTEMPTS,
                exc,
            )
            if attempt < CALLBACK_MAX_ATTEMPTS:
                time.sleep(CALLBACK_RETRY_DELAY_SEC)
    logger.error(
        "Callback exhausted retries for job=%s: %s",
        payload.get("job_id"),
        last_error,
    )


def _run_layout_job(job: QueueJob, *, parser_key: str) -> dict[str, Any]:
    """Run the official layout pipeline and persist all SDK artifacts."""
    if job.data is None:
        raise RuntimeError("Layout job has no file data")
    job_dir = _safe_job_dir(job.job_id)
    started = time.perf_counter()
    _update_metadata(job_dir, status="running", started_at=_beijing_now())
    try:
        result = _get_parser(parser_key).parse(job.data)
        # Official SDK saves Markdown/JSON, imgs/, and layout_vis/ when present.
        result.save(output_dir=str(job_dir))
        markdown = _extract_markdown(result)
        metadata = _update_metadata(
            job_dir,
            status="completed",
            chars=len(markdown),
            elapsed_sec=round(time.perf_counter() - started, 2),
            finished_at=_beijing_now(),
        )
        return _complete_response(job_dir, metadata, markdown)
    except Exception as exc:
        _update_metadata(
            job_dir,
            status="failed",
            elapsed_sec=round(time.perf_counter() - started, 2),
            finished_at=_beijing_now(),
            error=str(exc),
        )
        logger.exception("Layout parse failed for %s (job=%s)", job.filename, job.job_id)
        raise


def _run_model_only_job(job: QueueJob) -> dict[str, Any]:
    """Send one image directly to vLLM and persist text plus raw response."""
    if job.data is None:
        raise RuntimeError("Model-only job has no file data")
    job_dir = _safe_job_dir(job.job_id)
    started = time.perf_counter()
    _update_metadata(job_dir, status="running", started_at=_beijing_now())
    suffix = Path(job.filename).suffix.lower()
    mime = MIME_BY_SUFFIX.get(suffix, "image/png")
    image_url = f"data:{mime};base64,{base64.b64encode(job.data).decode('ascii')}"
    payload = {
        "model": _vllm_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": "Text Recognition:"},
            ],
        }],
        "max_tokens": _vllm_max_tokens,
        "temperature": 0.0,
    }
    try:
        response = requests.post(_vllm_url, json=payload, timeout=_vllm_timeout)
        response.raise_for_status()
        raw_result = response.json()
        markdown = raw_result["choices"][0]["message"]["content"]
        (job_dir / "result.md").write_text(markdown, encoding="utf-8")
        (job_dir / "result.json").write_text(
            json.dumps(raw_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        metadata = _update_metadata(
            job_dir,
            status="completed",
            chars=len(markdown),
            elapsed_sec=round(time.perf_counter() - started, 2),
            finished_at=_beijing_now(),
        )
        return _complete_response(job_dir, metadata, markdown)
    except Exception as exc:
        _update_metadata(
            job_dir,
            status="failed",
            elapsed_sec=round(time.perf_counter() - started, 2),
            finished_at=_beijing_now(),
            error=str(exc),
        )
        logger.exception("Model-only OCR failed for %s (job=%s)", job.filename, job.job_id)
        raise


def _run_oss_job(job: QueueJob, runner: Any) -> dict[str, Any]:
    """Download from OSS when the worker starts, parse, upload, then callback."""
    if not job.oss_path:
        raise RuntimeError("OSS job is missing oss_path")
    job_dir = _safe_job_dir(job.job_id)
    started = time.perf_counter()
    oss_output_prefix = f"{_oss_prefix.strip('/')}/{job.job_id}/output/attempt-{job.attempt}"
    try:
        data, filename = _download_from_oss(job.oss_path)
        if not data:
            raise ValueError("Downloaded file is empty")
        actual_type, _ = _detect_content_type(data, filename)
        if actual_type != job.content_type:
            raise ValueError(
                f"Downloaded content type {actual_type!r} does not match "
                f"path suffix type {job.content_type!r}"
            )
        job.data = data
        job.filename = filename
        _update_metadata(
            job_dir,
            filename=filename,
            status="running",
            started_at=_beijing_now(),
        )
        result = runner(job)
        oss_artifacts = _upload_to_oss(job_dir, oss_output_prefix=oss_output_prefix)
        metadata = _update_metadata(
            job_dir,
            oss_artifacts=oss_artifacts,
            oss_uploaded=True,
            oss_uploaded_at=_beijing_now(),
            oss_prefix=oss_output_prefix,
        )
        callback_payload = {
            "job_id": job.job_id,
            "attempt": job.attempt,
            "status": "completed",
            "queue": job.queue_name,
            "chars": metadata.get("chars", result.get("chars", 0)),
            "elapsed_sec": metadata.get(
                "elapsed_sec",
                round(time.perf_counter() - started, 2),
            ),
            "oss_prefix": oss_output_prefix,
            "oss_artifacts": oss_artifacts,
            "finished_at": metadata.get("finished_at", _beijing_now()),
        }
        _post_callback(callback_payload)
        return {**result, **callback_payload}
    except Exception as exc:
        finished_at = _beijing_now()
        _update_metadata(
            job_dir,
            status="failed",
            elapsed_sec=round(time.perf_counter() - started, 2),
            finished_at=finished_at,
            error=str(exc),
        )
        _post_callback({
            "job_id": job.job_id,
            "attempt": job.attempt,
            "status": "failed",
            "queue": job.queue_name,
            "error": str(exc),
            "finished_at": finished_at,
        })
        logger.exception("OSS parse failed for job=%s", job.job_id)
        raise


async def _queue_worker(
    queue: asyncio.Queue[QueueJob],
    runner: Any,
    worker_name: str,
    queue_name: str,
) -> None:
    while True:
        job = await queue.get()
        _adjust_running(queue_name, 1)
        try:
            if job.oss_path is not None:
                await asyncio.to_thread(_run_oss_job, job, runner)
            else:
                result = await asyncio.to_thread(runner, job)
                if job.future is not None and not job.future.cancelled():
                    job.future.set_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if job.future is not None and not job.future.cancelled():
                job.future.set_exception(exc)
        finally:
            _adjust_running(queue_name, -1)
            queue.task_done()
            logger.info("%s finished job=%s", worker_name, job.job_id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _pdf_queue, _image_layout_queue, _model_queue
    _pdf_queue = asyncio.Queue(maxsize=_pdf_queue_capacity)
    _image_layout_queue = asyncio.Queue(maxsize=_image_layout_queue_capacity)
    _model_queue = asyncio.Queue(maxsize=_model_queue_capacity)
    workers = [
        asyncio.create_task(
            _queue_worker(
                _pdf_queue,
                partial(_run_layout_job, parser_key=f"pdf-{index}"),
                f"pdf-layout-{index}",
                "pdf_layout",
            )
        )
        for index in range(_pdf_workers)
    ]
    workers.extend(
        asyncio.create_task(
            _queue_worker(
                _image_layout_queue,
                partial(_run_layout_job, parser_key=f"image-{index}"),
                f"image-layout-{index}",
                "image_layout",
            )
        )
        for index in range(_image_layout_workers)
    )
    workers.extend(
        asyncio.create_task(
            _queue_worker(
                _model_queue,
                _run_model_only_job,
                f"model-{index}",
                "model_only",
            )
        )
        for index in range(_model_workers)
    )
    logger.info(
        "Queues ready: PDF layout capacity=%d workers=%d; "
        "image layout capacity=%d workers=%d; model capacity=%d workers=%d",
        _pdf_queue_capacity,
        _pdf_workers,
        _image_layout_queue_capacity,
        _image_layout_workers,
        _model_queue_capacity,
        _model_workers,
    )
    try:
        yield
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


app = FastAPI(
    title="GLM-OCR Queued Parse Service",
    version="2.2.0",
    lifespan=lifespan,
)


async def _enqueue(
    file: UploadFile,
    *,
    image_mode: Literal["auto", "layout", "model_only"],
    images_only: bool,
) -> JSONResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        content_type, _ = _detect_content_type(data, file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if images_only and content_type != "image":
        raise HTTPException(status_code=400, detail="Only image files are accepted")
    if content_type == "pdf" and image_mode == "model_only":
        raise HTTPException(
            status_code=400,
            detail="PDF requires layout mode; model_only accepts images only",
        )

    # Medical/full-page images use layout by default. Cropped images must opt in
    # to model_only explicitly.
    mode: Literal["layout", "model_only"] = (
        "model_only" if content_type == "image" and image_mode == "model_only"
        else "layout"
    )
    if mode == "model_only":
        queue = _model_queue
        queue_name = "model_only"
    elif content_type == "pdf":
        queue = _pdf_queue
        queue_name = "pdf_layout"
    else:
        queue = _image_layout_queue
        queue_name = "image_layout"
    if queue is None:
        raise HTTPException(status_code=503, detail="Service queue is not ready")
    if queue.full():
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"{queue_name} queue is full",
                "retry_after_sec": 10,
            },
            headers={"Retry-After": "10"},
        )

    job_id = str(uuid.uuid4())
    job_dir = _safe_job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=False)
    created = _beijing_now()
    _write_metadata(job_dir, {
        "job_id": job_id,
        "status": "queued",
        "queue": queue_name,
        "filename": file.filename,
        "content_type": content_type,
        "image_mode": image_mode,
        "created_at": created,
        "output_dir": str(job_dir),
    })
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    job = QueueJob(
        job_id=job_id,
        filename=file.filename,
        content_type=content_type,
        mode=mode,
        queue_name=queue_name,
        data=data,
        future=future,
    )
    try:
        queue.put_nowait(job)
    except asyncio.QueueFull as exc:
        _update_metadata(
            job_dir,
            status="rejected",
            error=f"{queue_name} queue is full",
        )
        raise HTTPException(
            status_code=429,
            detail={"message": f"{queue_name} queue is full", "job_id": job_id},
            headers={"Retry-After": "10"},
        ) from exc

    logger.info(
        "Queued %s (%d bytes, job=%s, queue=%s, depth=%d)",
        file.filename,
        len(data),
        job_id,
        queue_name,
        queue.qsize(),
    )
    try:
        return JSONResponse(await future)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"message": "Parse failed", "job_id": job_id, "error": str(exc)},
        ) from exc


def _queue_snapshot(
    queue: asyncio.Queue[QueueJob] | None,
    *,
    capacity: int,
    workers: int,
    queue_name: str,
) -> dict[str, Any]:
    waiting = queue.qsize() if queue else 0
    running = _running_count(queue_name)
    available = capacity - waiting
    return {
        "capacity": capacity,
        "workers": workers,
        "waiting": waiting,
        "running": running,
        "available_slots": max(available, 0),
        "usage_percent": round(waiting / capacity * 100, 1) if capacity else 0,
        "is_full": queue.full() if queue else False,
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": "glm-ocr",
        "service": "glm-ocr-queued-parse-v2",
        "output_root": str(_output_root.resolve()),
        "queues": {
            "pdf_layout": _queue_snapshot(
                _pdf_queue,
                capacity=_pdf_queue_capacity,
                workers=_pdf_workers,
                queue_name="pdf_layout",
            ),
            "image_layout": _queue_snapshot(
                _image_layout_queue,
                capacity=_image_layout_queue_capacity,
                workers=_image_layout_workers,
                queue_name="image_layout",
            ),
            "model_only": _queue_snapshot(
                _model_queue,
                capacity=_model_queue_capacity,
                workers=_model_workers,
                queue_name="model_only",
            ),
        },
    }


@app.get("/queue_status")
async def queue_status() -> dict[str, Any]:
    queues = {
        "pdf_layout": _queue_snapshot(
            _pdf_queue,
            capacity=_pdf_queue_capacity,
            workers=_pdf_workers,
            queue_name="pdf_layout",
        ),
        "image_layout": _queue_snapshot(
            _image_layout_queue,
            capacity=_image_layout_queue_capacity,
            workers=_image_layout_workers,
            queue_name="image_layout",
        ),
        "model_only": _queue_snapshot(
            _model_queue,
            capacity=_model_queue_capacity,
            workers=_model_workers,
            queue_name="model_only",
        ),
    }
    total_waiting = sum(q["waiting"] for q in queues.values())
    total_running = sum(q["running"] for q in queues.values())
    total_capacity = sum(q["capacity"] for q in queues.values())
    return {
        "status": "ok",
        "timestamp": _beijing_now(),
        "total_waiting": total_waiting,
        "total_running": total_running,
        "total_capacity": total_capacity,
        "queues": queues,
    }


@app.post("/parse")
async def parse_document(
    file: UploadFile = File(..., description="PDF or image file"),
    image_mode: Literal["auto", "layout", "model_only"] = Query("auto"),
) -> JSONResponse:
    return await _enqueue(file, image_mode=image_mode, images_only=False)


@app.post("/parse_image")
async def parse_image(
    file: UploadFile = File(..., description="Image file"),
    image_mode: Literal["auto", "layout", "model_only"] = Query("auto"),
) -> JSONResponse:
    return await _enqueue(file, image_mode=image_mode, images_only=True)


@app.get("/results/{job_id}")
async def get_result(job_id: str) -> JSONResponse:
    job_dir = _safe_job_dir(job_id)
    metadata_file = job_dir / "job.json"
    if not metadata_file.is_file():
        raise HTTPException(status_code=404, detail="Result not found")
    metadata = _read_metadata(job_dir)
    metadata["artifacts"] = _relative_files(job_dir)
    return JSONResponse(metadata)


@app.get("/results/{job_id}/{artifact_path:path}")
async def download_artifact(job_id: str, artifact_path: str) -> FileResponse:
    job_dir = _safe_job_dir(job_id)
    requested = (job_dir / artifact_path).resolve()
    if not requested.is_relative_to(job_dir) or not requested.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(requested, filename=requested.name)


class OssTaskRequest(BaseModel):
    job_id: str
    oss_path: str
    image_mode: Literal["auto", "layout", "model_only"] = "auto"
    attempt: int = Field(default=1, ge=1)


@app.post("/parse_oss")
async def parse_oss_task(req: OssTaskRequest) -> JSONResponse:
    if not _oss_configured():
        raise HTTPException(
            status_code=503,
            detail="OSS is not configured; set credentials in the service environment",
        )
    # Validate job_id early via path helper.
    job_dir = _safe_job_dir(req.job_id)
    try:
        content_type = _infer_content_type_from_path(req.oss_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if content_type == "pdf" and req.image_mode == "model_only":
        raise HTTPException(
            status_code=400,
            detail="PDF requires layout mode; model_only accepts images only",
        )
    mode: Literal["layout", "model_only"] = (
        "model_only" if content_type == "image" and req.image_mode == "model_only"
        else "layout"
    )
    if mode == "model_only":
        queue = _model_queue
        queue_name = "model_only"
    elif content_type == "pdf":
        queue = _pdf_queue
        queue_name = "pdf_layout"
    else:
        queue = _image_layout_queue
        queue_name = "image_layout"
    if queue is None:
        raise HTTPException(status_code=503, detail="Service queue is not ready")

    filename = Path(req.oss_path).name or "input.bin"
    created = _beijing_now()
    job_dir.mkdir(parents=True, exist_ok=True)
    _write_metadata(job_dir, {
        "job_id": req.job_id,
        "status": "queued",
        "queue": queue_name,
        "filename": filename,
        "content_type": content_type,
        "image_mode": req.image_mode,
        "attempt": req.attempt,
        "oss_source": req.oss_path,
        "created_at": created,
        "output_dir": str(job_dir),
    })
    job = QueueJob(
        job_id=req.job_id,
        filename=filename,
        content_type=content_type,
        mode=mode,
        queue_name=queue_name,
        data=None,
        oss_path=req.oss_path,
        attempt=req.attempt,
        image_mode=req.image_mode,
        future=None,
    )
    try:
        queue.put_nowait(job)
    except asyncio.QueueFull as exc:
        _update_metadata(
            job_dir,
            status="rejected",
            error=f"{queue_name} queue is full",
        )
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"{queue_name} queue is full",
                "job_id": req.job_id,
                "retry_after_sec": 10,
            },
            headers={"Retry-After": "10"},
        ) from exc

    logger.info(
        "Accepted OSS task job=%s path=%s queue=%s attempt=%d depth=%d",
        req.job_id,
        req.oss_path,
        queue_name,
        req.attempt,
        queue.qsize(),
    )
    return JSONResponse(
        status_code=202,
        content={
            "job_id": req.job_id,
            "status": "accepted",
            "queue": queue_name,
            "attempt": req.attempt,
            "oss_source": req.oss_path,
            "created_at": created,
        },
    )


def main() -> None:
    arg_parser = argparse.ArgumentParser(description="Queued GLM-OCR Parse Service v2")
    arg_parser.add_argument(
        "--service-config",
        default=os.environ.get(
            "GLM_OCR_V2_SERVICE_CONFIG",
            str(DEFAULT_SERVICE_CONFIG),
        ),
    )
    arg_parser.add_argument(
        "--host", default=None
    )
    arg_parser.add_argument(
        "--port",
        type=int,
        default=None,
    )
    arg_parser.add_argument(
        "--config", default=None
    )
    arg_parser.add_argument(
        "--layout-device",
        default=None,
    )
    arg_parser.add_argument(
        "--output-root",
        default=None,
    )
    args = arg_parser.parse_args()

    global _parser_config_path, _parser_layout_device, _output_root
    global _pdf_queue_capacity, _image_layout_queue_capacity, _model_queue_capacity
    global _pdf_workers, _image_layout_workers, _model_workers
    global _vllm_url, _vllm_model, _vllm_timeout, _vllm_max_tokens
    global _oss_endpoint, _oss_access_key_id, _oss_access_key_secret
    global _oss_bucket_name, _oss_prefix, _oss_signed_url_expires, _callback_url
    service_config_path = Path(args.service_config).expanduser().resolve()
    if not service_config_path.is_file():
        raise FileNotFoundError(f"GLM-OCR v2 service config not found: {service_config_path}")
    service_config = yaml.safe_load(service_config_path.read_text(encoding="utf-8")) or {}
    service = service_config.get("service", {})
    queues = service_config.get("queues", {})
    vllm = service_config.get("vllm", {})

    def config_path(value: str | None, fallback: Path) -> Path:
        path = Path(value) if value else fallback
        if not path.is_absolute():
            path = REPOSITORY_ROOT / path
        return path.expanduser().resolve()

    host = args.host or str(service.get("host", "127.0.0.1"))
    port = args.port or int(service.get("port", DEFAULT_PORT))
    _parser_config_path = config_path(args.config or service.get("sdk_config"), DEFAULT_CONFIG)
    _parser_layout_device = args.layout_device or str(
        service.get("layout_device", "cuda:1")
    )
    _output_root = config_path(
        args.output_root or service.get("output_root"),
        DEFAULT_OUTPUT_ROOT,
    )
    _output_root.mkdir(parents=True, exist_ok=True)

    pdf_queue = queues.get("pdf_layout", {})
    image_layout_queue = queues.get("image_layout", {})
    model_queue = queues.get("model_only", {})
    _pdf_queue_capacity = int(pdf_queue.get("capacity", 4))
    _image_layout_queue_capacity = int(image_layout_queue.get("capacity", 8))
    _model_queue_capacity = int(model_queue.get("capacity", 8))
    _pdf_workers = int(pdf_queue.get("workers", 2))
    _image_layout_workers = int(image_layout_queue.get("workers", 1))
    _model_workers = int(model_queue.get("workers", 4))
    _vllm_url = str(
        vllm.get("url", "http://127.0.0.1:18080/v1/chat/completions")
    )
    _vllm_model = str(vllm.get("model", "glm-ocr"))
    _vllm_timeout = int(vllm.get("timeout", 300))
    _vllm_max_tokens = int(vllm.get("max_tokens", 4096))

    _oss_endpoint = os.environ.get("OSS_ENDPOINT", "")
    _oss_access_key_id = os.environ.get("OSS_ACCESS_KEY_ID", "")
    _oss_access_key_secret = os.environ.get("OSS_ACCESS_KEY_SECRET", "")
    _oss_bucket_name = os.environ.get("OSS_BUCKET_NAME", "")
    _oss_prefix = os.environ.get("OSS_PREFIX", "glm_ocr_output")
    _oss_signed_url_expires = int(os.environ.get("OSS_SIGNED_URL_EXPIRES_SECONDS", "3600"))
    _callback_url = os.environ.get("GLM_OCR_V2_CALLBACK_URL", "").rstrip("/")
    if _oss_configured():
        logger.info(
            "OSS configured: endpoint=%s bucket=%s prefix=%s callback=%s",
            _oss_endpoint,
            _oss_bucket_name,
            _oss_prefix,
            bool(_callback_url),
        )
    else:
        logger.info(
            "OSS not configured; /parse_oss unavailable until credentials are "
            "set in the service environment"
        )

    import uvicorn

    logger.info(
        "Starting GLM-OCR v2 on %s:%d (output=%s)",
        host,
        port,
        _output_root,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
