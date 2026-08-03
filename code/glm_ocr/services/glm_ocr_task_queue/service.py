#!/usr/bin/env python3
"""Outer asynchronous persistent queue for the existing GLM-OCR v2 API."""

from __future__ import annotations

import argparse
import asyncio
import logging
import mimetypes
import os
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import requests
import yaml
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

try:
    from .storage import ResultStore, build_result_store
    from .task_queue import (
        TERMINAL_STATUSES,
        QueueCapacityError,
        TaskDispatcher,
        TaskRepository,
        beijing_now,
        normalize_task_name,
    )
except ImportError:
    from storage import ResultStore, build_result_store
    from task_queue import (
        TERMINAL_STATUSES,
        QueueCapacityError,
        TaskDispatcher,
        TaskRepository,
        beijing_now,
        normalize_task_name,
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("glm-ocr-task-queue")

GLM_OCR_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG = GLM_OCR_ROOT / "config" / "task_queue_v2.yaml"
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT / "result" / "glm_ocr" / "task_queue"
DEFAULT_PORT = 18092

SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
SUPPORTED_SUFFIXES = SUPPORTED_IMAGE_SUFFIXES | {".pdf"}

_repository: TaskRepository | None = None
_result_store: ResultStore | None = None
_pdf_dispatcher: TaskDispatcher | None = None
_image_dispatcher: TaskDispatcher | None = None
_scheduler_event = threading.Event()
_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None

_inner_parse_url = "http://127.0.0.1:18091/parse"
_inner_timeout = 3600
_pdf_capacity = 4
_pdf_workers = 2
_image_capacity = 8
_image_workers = 1


def _services() -> tuple[TaskRepository, ResultStore]:
    if _repository is None or _result_store is None:
        raise RuntimeError("Task service is not configured")
    return _repository, _result_store


def _dispatcher(queue_name: str) -> TaskDispatcher:
    if queue_name == "pdf":
        dispatcher = _pdf_dispatcher
    elif queue_name == "image":
        dispatcher = _image_dispatcher
    else:
        raise RuntimeError(f"Unknown task queue: {queue_name}")
    if dispatcher is None:
        raise RuntimeError("Task dispatcher is not ready")
    return dispatcher


def _safe_filename(filename: str) -> str:
    normalized = Path(filename.replace("\\", "/")).name
    return normalized if normalized not in {"", ".", ".."} else "input.bin"


def _detect_content_type(header: bytes, filename: str) -> Literal["pdf", "image"]:
    if header.startswith(b"%PDF-"):
        return "pdf"
    suffix = Path(filename).suffix.lower()
    if suffix in SUPPORTED_IMAGE_SUFFIXES:
        return "image"
    accepted = ", ".join(sorted(SUPPORTED_SUFFIXES))
    raise ValueError(f"Unsupported file type. Accepted: {accepted}")


def _copy_upload(source: Any, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.seek(0)
    with destination.open("wb") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)
    return destination.stat().st_size


def _task_urls(job_id: str) -> dict[str, str]:
    return {
        "status_url": f"/tasks/{job_id}/status",
        "result_url": f"/tasks/{job_id}/result",
        "cancel_url": f"/tasks/{job_id}",
    }


def _load_task(job_id: str) -> tuple[TaskRepository, dict[str, Any]]:
    repository, _ = _services()
    try:
        metadata = repository.read(job_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    return repository, metadata


def _claim_task(repository: TaskRepository, job_id: str) -> dict[str, Any] | None:
    claimed = False

    def mutation(metadata: dict[str, Any]) -> None:
        nonlocal claimed
        status = metadata.get("status")
        if status in {"canceled", "cancel_requested"}:
            metadata.update(
                status="canceled",
                finished_at=metadata.get("finished_at") or beijing_now(),
            )
            return
        if status in TERMINAL_STATUSES:
            return
        metadata.update(status="running", started_at=beijing_now())
        claimed = True

    metadata = repository.mutate(job_id, mutation)
    return metadata if claimed else None


def _mark_canceled_if_requested(
    repository: TaskRepository,
    job_id: str,
    *,
    started: float,
) -> bool:
    canceled = False

    def mutation(metadata: dict[str, Any]) -> None:
        nonlocal canceled
        if metadata.get("status") in {"cancel_requested", "canceled"}:
            metadata.update(
                status="canceled",
                elapsed_sec=round(time.perf_counter() - started, 2),
                finished_at=beijing_now(),
            )
            canceled = True

    repository.mutate(job_id, mutation)
    return canceled


def _process_job(job_id: str) -> None:
    """Dedicated Worker thread: call the inner v2 API and persist outer results."""
    repository, result_store = _services()
    metadata = _claim_task(repository, job_id)
    if metadata is None:
        return

    started = time.perf_counter()
    try:
        source = repository.input_path(metadata)
        mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        with source.open("rb") as input_file:
            response = requests.post(
                _inner_parse_url,
                params={"image_mode": metadata.get("image_mode", "auto")},
                files={"file": (metadata["filename"], input_file, mime)},
                timeout=_inner_timeout,
            )
        response.raise_for_status()
        inner_response = response.json()
        text = str(inner_response.get("text", ""))

        if _mark_canceled_if_requested(
            repository,
            job_id,
            started=started,
        ):
            return

        storage_fields = result_store.save(
            job_dir=repository.job_dir(job_id),
            text=text,
            inner_response=inner_response,
        )

        def complete(current: dict[str, Any]) -> None:
            if current.get("status") in {"cancel_requested", "canceled"}:
                current.update(
                    status="canceled",
                    elapsed_sec=round(time.perf_counter() - started, 2),
                    finished_at=beijing_now(),
                )
                return
            current.update(
                status="completed",
                chars=len(text),
                elapsed_sec=round(time.perf_counter() - started, 2),
                finished_at=beijing_now(),
                inner_job_id=inner_response.get("job_id"),
                inner_result_url=inner_response.get("result_url"),
                **storage_fields,
            )

        repository.mutate(job_id, complete)
        logger.info("Task completed: %s", job_id)
    except Exception as exc:
        def fail(current: dict[str, Any]) -> None:
            canceled = current.get("status") in {
                "cancel_requested",
                "canceled",
            }
            current.update(
                status="canceled" if canceled else "failed",
                elapsed_sec=round(time.perf_counter() - started, 2),
                finished_at=beijing_now(),
            )
            if not canceled:
                current["error"] = str(exc)

        repository.mutate(job_id, fail)
        logger.exception("Task failed: %s", job_id)


def _recover_tasks() -> int:
    repository, _ = _services()
    recovered = 0
    for metadata in repository.iter_metadata():
        job_id = str(metadata.get("job_id", ""))
        status = metadata.get("status")
        if status == "cancel_requested":
            repository.update(
                job_id,
                status="canceled",
                finished_at=beijing_now(),
                recovery_note="Canceled during service restart",
            )
            continue
        if status not in {"pending", "queued", "running"}:
            continue
        try:
            repository.input_path(metadata)
            queue_name = (
                "pdf" if metadata.get("content_type") == "pdf" else "image"
            )
            repository.update(
                job_id,
                status="pending",
                queue=queue_name,
                recovered_at=beijing_now(),
                recovery_count=int(metadata.get("recovery_count", 0)) + 1,
            )
            recovered += 1
        except Exception as exc:
            repository.update(
                job_id,
                status="failed",
                finished_at=beijing_now(),
                error=f"Recovery failed: {exc}",
            )
    return recovered


class _TaskStateChanged(RuntimeError):
    pass


def _wake_scheduler() -> None:
    _scheduler_event.set()


def _mark_ready(repository: TaskRepository, job_id: str) -> None:
    def mutation(metadata: dict[str, Any]) -> None:
        if metadata.get("status") != "pending":
            raise _TaskStateChanged(job_id)
        metadata.update(status="queued", queued_at=beijing_now())

    repository.mutate(job_id, mutation)


def _scheduler_loop() -> None:
    """Fill the bounded ready queues from the durable, unbounded backlog."""
    repository, _ = _services()
    while not _scheduler_stop.is_set():
        _scheduler_event.wait(timeout=0.5)
        _scheduler_event.clear()
        if _scheduler_stop.is_set():
            return

        made_progress = True
        while made_progress and not _scheduler_stop.is_set():
            made_progress = False
            records = sorted(
                repository.iter_metadata(),
                key=lambda item: str(item.get("created_at", "")),
            )
            for metadata in records:
                if metadata.get("status") != "pending":
                    continue
                job_id = str(metadata["job_id"])
                queue_name = str(metadata["queue"])
                try:
                    _dispatcher(queue_name).submit(
                        job_id,
                        on_enqueued=lambda task_id=job_id: _mark_ready(
                            repository,
                            task_id,
                        ),
                    )
                    made_progress = True
                except QueueCapacityError:
                    continue
                except _TaskStateChanged:
                    continue
                except Exception:
                    logger.exception("Could not schedule persisted task: %s", job_id)


def _backlog_snapshot() -> dict[str, int]:
    repository, _ = _services()
    counts = {"pdf": 0, "image": 0}
    for metadata in repository.iter_metadata():
        if metadata.get("status") == "pending":
            queue_name = str(metadata.get("queue"))
            if queue_name in counts:
                counts[queue_name] += 1
    return counts


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _pdf_dispatcher, _image_dispatcher, _scheduler_thread
    _services()
    _scheduler_stop.clear()
    _scheduler_event.clear()
    _pdf_dispatcher = TaskDispatcher(
        name="pdf",
        capacity=_pdf_capacity,
        workers=_pdf_workers,
        runner=_process_job,
        on_slot_available=_wake_scheduler,
        logger=logger,
    )
    _image_dispatcher = TaskDispatcher(
        name="image",
        capacity=_image_capacity,
        workers=_image_workers,
        runner=_process_job,
        on_slot_available=_wake_scheduler,
        logger=logger,
    )
    _pdf_dispatcher.start()
    _image_dispatcher.start()
    recovered = _recover_tasks()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        name="persistent-task-scheduler",
        daemon=True,
    )
    _scheduler_thread.start()
    _wake_scheduler()
    logger.info(
        "Task queues ready: PDF workers=%d queue=%d; "
        "image workers=%d queue=%d; recovered=%d",
        _pdf_workers,
        _pdf_capacity,
        _image_workers,
        _image_capacity,
        recovered,
    )
    try:
        yield
    finally:
        _scheduler_stop.set()
        _wake_scheduler()
        if _scheduler_thread is not None:
            _scheduler_thread.join(timeout=1)
        _pdf_dispatcher.stop()
        _image_dispatcher.stop()


app = FastAPI(
    title="GLM-OCR Async Persistent Task Queue",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    backlog = _backlog_snapshot() if _repository is not None else {
        "pdf": 0,
        "image": 0,
    }
    return {
        "status": "ok",
        "service": "glm-ocr-task-queue",
        "inner_parse_url": _inner_parse_url,
        "queues": {
            "pdf": (
                {
                    **_pdf_dispatcher.snapshot(),
                    "backlog": backlog["pdf"],
                }
                if _pdf_dispatcher is not None
                else {
                    "capacity": _pdf_capacity,
                    "workers": _pdf_workers,
                    "waiting": 0,
                    "running": 0,
                    "backlog": backlog["pdf"],
                }
            ),
            "image": (
                {
                    **_image_dispatcher.snapshot(),
                    "backlog": backlog["image"],
                }
                if _image_dispatcher is not None
                else {
                    "capacity": _image_capacity,
                    "workers": _image_workers,
                    "waiting": 0,
                    "running": 0,
                    "backlog": backlog["image"],
                }
            ),
        },
    }


@app.post("/tasks")
async def submit_task(
    file: UploadFile = File(..., description="PDF or image file"),
    image_mode: Literal["auto", "layout", "model_only"] = Query("auto"),
) -> JSONResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    filename = _safe_filename(file.filename)
    task_name = Path(filename).stem
    header = await file.read(16)
    await file.seek(0)
    if not header:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        normalize_task_name(task_name)
        content_type = _detect_content_type(header, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if content_type == "pdf" and image_mode == "model_only":
        raise HTTPException(
            status_code=400,
            detail="PDF requires auto or layout mode",
        )

    queue_name = "pdf" if content_type == "pdf" else "image"
    _dispatcher(queue_name)

    repository, _ = _services()
    job_id, job_dir = repository.create(task_name)
    relative_input = Path("input") / filename
    created_at = beijing_now()
    try:
        size_bytes = await asyncio.to_thread(
            _copy_upload,
            file.file,
            job_dir / relative_input,
        )
    except Exception as exc:
        repository.write(job_id, {
            "job_id": job_id,
            "name": task_name,
            "status": "failed",
            "filename": filename,
            "created_at": created_at,
            "finished_at": beijing_now(),
            "error": f"Could not persist upload: {exc}",
        })
        raise HTTPException(
            status_code=500,
            detail={"message": "Could not persist upload", "job_id": job_id},
        ) from exc

    repository.write(job_id, {
        "job_id": job_id,
        "name": task_name,
        "status": "pending",
        "queue": queue_name,
        "filename": filename,
        "input_file": str(relative_input),
        "size_bytes": size_bytes,
        "content_type": content_type,
        "image_mode": image_mode,
        "created_at": created_at,
        "output_dir": str(job_dir),
    })
    _wake_scheduler()
    metadata = repository.read(job_id)
    return JSONResponse(
        status_code=202,
        content={**metadata, **_task_urls(job_id)},
    )


@app.get("/tasks/{job_id}/status")
async def get_task_status(job_id: str) -> JSONResponse:
    repository, metadata = _load_task(job_id)
    return JSONResponse({
        **metadata,
        "artifacts": repository.artifacts(job_id),
        **_task_urls(job_id),
    })


@app.get("/tasks/{job_id}/result")
async def get_task_result(job_id: str) -> JSONResponse:
    repository, metadata = _load_task(job_id)
    if metadata.get("status") != "completed":
        return JSONResponse(
            status_code=409,
            content={
                **metadata,
                "message": "Task result is not available",
                **_task_urls(job_id),
            },
        )
    _, result_store = _services()
    try:
        text = result_store.read_text(
            job_dir=repository.job_dir(job_id),
            metadata=metadata,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="Result file is missing") from exc
    return JSONResponse({
        **metadata,
        "text": text,
        "artifacts": repository.artifacts(job_id),
        **_task_urls(job_id),
    })


@app.delete("/tasks/{job_id}")
async def cancel_task(job_id: str) -> JSONResponse:
    repository, metadata = _load_task(job_id)
    current = metadata.get("status")
    if current in {"cancel_requested", "canceled"}:
        status_code = 202 if current == "cancel_requested" else 200
        return JSONResponse(
            status_code=status_code,
            content={**metadata, **_task_urls(job_id)},
        )
    if current in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Task in {current!r} state cannot be canceled",
        )

    if current == "pending":
        metadata = repository.update(
            job_id,
            status="canceled",
            cancel_requested_at=beijing_now(),
            finished_at=beijing_now(),
        )
        _wake_scheduler()
        return JSONResponse(
            status_code=200,
            content={**metadata, **_task_urls(job_id)},
        )

    dispatcher = _dispatcher(str(metadata.get("queue")))
    location = dispatcher.cancel(job_id)

    def cancel(current_metadata: dict[str, Any]) -> None:
        if (
            current_metadata.get("status") == "queued"
            and location in {"queued", "missing"}
        ):
            current_metadata.update(
                status="canceled",
                cancel_requested_at=beijing_now(),
                finished_at=beijing_now(),
            )
        else:
            current_metadata.update(
                status="cancel_requested",
                cancel_requested_at=beijing_now(),
            )

    metadata = repository.mutate(job_id, cancel)
    status_code = 200 if metadata["status"] == "canceled" else 202
    return JSONResponse(
        status_code=status_code,
        content={**metadata, **_task_urls(job_id)},
    )


def configure(config_path: Path) -> tuple[str, int]:
    global _repository, _result_store
    global _inner_parse_url, _inner_timeout
    global _pdf_capacity, _pdf_workers, _image_capacity, _image_workers

    resolved = config_path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Task queue config not found: {resolved}")
    config = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    service = config.get("service", {})
    inner = config.get("inner_service", {})
    queues = config.get("queues", {})

    output_value = Path(
        os.environ.get(
            "GLM_OCR_TASK_OUTPUT_ROOT",
            str(service.get("output_root", DEFAULT_OUTPUT_ROOT)),
        )
    )
    if not output_value.is_absolute():
        output_value = REPOSITORY_ROOT / output_value
    _repository = TaskRepository(output_value)
    _result_store = build_result_store(config.get("storage", {}))

    base_url = str(inner.get("base_url", "http://127.0.0.1:18091")).rstrip("/")
    _inner_parse_url = f"{base_url}/parse"
    _inner_timeout = int(inner.get("timeout", 3600))

    pdf = queues.get("pdf", {})
    image = queues.get("image", {})
    _pdf_capacity = int(pdf.get("capacity", 4))
    _pdf_workers = int(pdf.get("workers", 2))
    _image_capacity = int(image.get("capacity", 8))
    _image_workers = int(image.get("workers", 1))
    if (_pdf_capacity, _pdf_workers, _image_capacity, _image_workers) != (
        4,
        2,
        8,
        1,
    ):
        logger.warning(
            "Queue settings differ from recommended PDF 4/2 and image 8/1"
        )

    host = os.environ.get(
        "GLM_OCR_TASK_HOST",
        str(service.get("host", "127.0.0.1")),
    )
    port = int(
        os.environ.get(
            "GLM_OCR_TASK_PORT",
            service.get("port", DEFAULT_PORT),
        )
    )
    return host, port


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Outer persistent task queue for GLM-OCR v2"
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("GLM_OCR_TASK_CONFIG", str(DEFAULT_CONFIG)),
    )
    args = parser.parse_args()
    host, port = configure(Path(args.config))

    import uvicorn

    logger.info(
        "Starting GLM-OCR task queue on %s:%d (inner=%s)",
        host,
        port,
        _inner_parse_url,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
