"""Persistent task metadata and fixed-size worker dispatchers."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
JOB_ID_RE = re.compile(r"^[\w.-]{1,59}_\d{20}$", re.UNICODE)
TERMINAL_STATUSES = {"completed", "failed", "canceled"}


def beijing_now() -> str:
    return datetime.now(BEIJING_TIMEZONE).isoformat(timespec="seconds")


def normalize_task_name(name: str) -> str:
    normalized = re.sub(r"[^\w.-]+", "_", name.strip(), flags=re.UNICODE)
    normalized = normalized.strip("._-")[:59]
    if not normalized:
        raise ValueError("Task name must contain at least one letter or number")
    return normalized


class TaskRepository:
    """Store each task in its own directory with atomically updated metadata."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._id_lock = threading.Lock()

    def create(self, name: str) -> tuple[str, Path]:
        normalized = normalize_task_name(name)
        with self._id_lock:
            for _ in range(100):
                timestamp = datetime.now(BEIJING_TIMEZONE).strftime(
                    "%Y%m%d%H%M%S%f"
                )
                job_id = f"{normalized}_{timestamp}"
                job_dir = self.root / job_id
                try:
                    job_dir.mkdir(parents=True, exist_ok=False)
                except FileExistsError:
                    continue
                return job_id, job_dir
        raise RuntimeError("Could not generate a unique job ID")

    def job_dir(self, job_id: str) -> Path:
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        job_dir = (self.root / job_id).resolve()
        if not job_dir.is_relative_to(self.root):
            raise ValueError("Invalid job ID")
        return job_dir

    def exists(self, job_id: str) -> bool:
        try:
            return (self.job_dir(job_id) / "job.json").is_file()
        except ValueError:
            return False

    def read(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            path = self.job_dir(job_id) / "job.json"
            if not path.is_file():
                raise FileNotFoundError(job_id)
            return json.loads(path.read_text(encoding="utf-8"))

    def write(self, job_id: str, metadata: dict[str, Any]) -> None:
        with self._lock:
            job_dir = self.job_dir(job_id)
            temporary = job_dir / ".job.json.tmp"
            temporary.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, job_dir / "job.json")

    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        return self.mutate(job_id, lambda metadata: metadata.update(fields))

    def mutate(
        self,
        job_id: str,
        mutation: Callable[[dict[str, Any]], Any],
    ) -> dict[str, Any]:
        with self._lock:
            metadata = self.read(job_id)
            mutation(metadata)
            self.write(job_id, metadata)
            return metadata

    def iter_metadata(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for job_dir in sorted(self.root.iterdir()):
            if not job_dir.is_dir() or not (job_dir / "job.json").is_file():
                continue
            try:
                records.append(self.read(job_dir.name))
            except (ValueError, OSError, json.JSONDecodeError):
                continue
        return records

    def input_path(self, metadata: dict[str, Any]) -> Path:
        job_dir = self.job_dir(str(metadata["job_id"]))
        relative = metadata.get("input_file")
        if not isinstance(relative, str) or not relative:
            raise FileNotFoundError("Task input is missing from metadata")
        path = (job_dir / relative).resolve()
        if not path.is_relative_to(job_dir) or not path.is_file():
            raise FileNotFoundError(relative)
        return path

    def artifacts(self, job_id: str) -> list[str]:
        job_dir = self.job_dir(job_id)
        return sorted(
            str(path.relative_to(job_dir))
            for path in job_dir.rglob("*")
            if path.is_file() and path.name != ".job.json.tmp"
        )


class QueueCapacityError(RuntimeError):
    pass


TaskLocation = Literal["queued", "running", "missing"]


class TaskDispatcher:
    """A bounded waiting deque serviced by a fixed number of dedicated threads."""

    def __init__(
        self,
        *,
        name: str,
        capacity: int,
        workers: int,
        runner: Callable[[str], None],
        on_slot_available: Callable[[], None] | None = None,
        logger: logging.Logger | None = None,
    ):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.name = name
        self.capacity = capacity
        self.workers = workers
        self._runner = runner
        self._on_slot_available = on_slot_available
        self._logger = logger or logging.getLogger(__name__)
        self._pending: deque[str] = deque()
        self._active: set[str] = set()
        self._condition = threading.Condition()
        self._stopping = False
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        with self._condition:
            if self._threads:
                return
            self._stopping = False
            self._threads = [
                threading.Thread(
                    target=self._worker_loop,
                    name=f"{self.name}-worker-{index}",
                    daemon=True,
                )
                for index in range(self.workers)
            ]
        for thread in self._threads:
            thread.start()

    def stop(self, join_timeout: float = 1.0) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=join_timeout)

    def submit(
        self,
        job_id: str,
        on_enqueued: Callable[[], None] | None = None,
    ) -> None:
        with self._condition:
            if self._stopping:
                raise RuntimeError(f"{self.name} dispatcher is stopping")
            if len(self._pending) >= self.capacity:
                raise QueueCapacityError(f"{self.name} queue is full")
            self._pending.append(job_id)
            try:
                if on_enqueued is not None:
                    on_enqueued()
            except Exception:
                self._pending.remove(job_id)
                raise
            self._condition.notify()

    def cancel(self, job_id: str) -> TaskLocation:
        canceled = False
        with self._condition:
            try:
                self._pending.remove(job_id)
            except ValueError:
                if job_id in self._active:
                    return "running"
                return "missing"
            canceled = True
        if canceled:
            self._notify_slot_available()
        return "queued"

    def snapshot(self) -> dict[str, int]:
        with self._condition:
            return {
                "capacity": self.capacity,
                "workers": self.workers,
                "waiting": len(self._pending),
                "running": len(self._active),
            }

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._stopping or bool(self._pending)
                )
                if self._stopping:
                    return
                job_id = self._pending.popleft()
                self._active.add(job_id)
            self._notify_slot_available()
            try:
                self._runner(job_id)
            except Exception:
                self._logger.exception(
                    "%s worker failed job=%s",
                    self.name,
                    job_id,
                )
            finally:
                with self._condition:
                    self._active.discard(job_id)

    def _notify_slot_available(self) -> None:
        if self._on_slot_available is not None:
            try:
                self._on_slot_available()
            except Exception:
                self._logger.exception(
                    "%s slot callback failed",
                    self.name,
                )
