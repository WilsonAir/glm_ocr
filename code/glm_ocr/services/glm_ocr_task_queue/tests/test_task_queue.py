from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from services.glm_ocr_task_queue.storage import LocalResultStore, OssResultStore
from services.glm_ocr_task_queue.task_queue import (
    JOB_ID_RE,
    QueueCapacityError,
    TaskDispatcher,
    TaskRepository,
    normalize_task_name,
)


class TaskRepositoryTests(unittest.TestCase):
    def test_job_id_uses_normalized_file_stem_and_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = TaskRepository(Path(temporary))
            job_id, job_dir = repository.create(" patient / 001 ")

            self.assertTrue(JOB_ID_RE.fullmatch(job_id))
            self.assertTrue(job_id.startswith("patient_001_"))
            self.assertTrue(job_dir.is_dir())

    def test_same_second_name_collision_uses_a_short_sequence_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = TaskRepository(Path(temporary))
            with patch("services.glm_ocr_task_queue.task_queue.datetime") as clock:
                clock.now.return_value = datetime(2026, 8, 4, 14, 38, 22)
                first_id, _ = repository.create("document")
                second_id, _ = repository.create("document")

            self.assertEqual(first_id, "document_20260804143822")
            self.assertEqual(second_id, "document_20260804143822_0001")
            self.assertTrue(JOB_ID_RE.fullmatch("document_20260804143822613423"))

    def test_metadata_write_is_readable_and_atomic_temp_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = TaskRepository(Path(temporary))
            job_id, job_dir = repository.create("case")
            repository.write(job_id, {"job_id": job_id, "status": "queued"})
            repository.update(job_id, status="running")

            self.assertEqual(repository.read(job_id)["status"], "running")
            self.assertFalse((job_dir / ".job.json.tmp").exists())
            json.loads((job_dir / "job.json").read_text(encoding="utf-8"))

    def test_invalid_file_stem_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_task_name(" / ")


class TaskDispatcherTests(unittest.TestCase):
    def test_two_workers_and_four_waiting_slots(self) -> None:
        release = threading.Event()
        first_two_started = threading.Event()
        lock = threading.Lock()
        active = 0
        max_active = 0
        started = 0

        def runner(_: str) -> None:
            nonlocal active, max_active, started
            with lock:
                active += 1
                started += 1
                max_active = max(max_active, active)
                if started >= 2:
                    first_two_started.set()
            release.wait(timeout=2)
            with lock:
                active -= 1

        dispatcher = TaskDispatcher(
            name="pdf",
            capacity=4,
            workers=2,
            runner=runner,
        )
        dispatcher.start()
        try:
            dispatcher.submit("running-1")
            dispatcher.submit("running-2")
            self.assertTrue(first_two_started.wait(timeout=1))

            for index in range(4):
                dispatcher.submit(f"waiting-{index}")
            with self.assertRaises(QueueCapacityError):
                dispatcher.submit("overflow")

            snapshot = dispatcher.snapshot()
            self.assertEqual(snapshot["workers"], 2)
            self.assertEqual(snapshot["running"], 2)
            self.assertEqual(snapshot["waiting"], 4)

            self.assertEqual(dispatcher.cancel("waiting-0"), "queued")
            dispatcher.submit("replacement")
            self.assertEqual(dispatcher.snapshot()["waiting"], 4)
        finally:
            release.set()
            deadline = time.time() + 2
            while dispatcher.snapshot()["running"] and time.time() < deadline:
                time.sleep(0.01)
            dispatcher.stop()

        self.assertEqual(max_active, 2)

    def test_one_image_worker_processes_one_image_at_a_time(self) -> None:
        release = threading.Event()
        first_started = threading.Event()
        lock = threading.Lock()
        active = 0
        max_active = 0

        def runner(_: str) -> None:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                first_started.set()
            release.wait(timeout=2)
            with lock:
                active -= 1

        dispatcher = TaskDispatcher(
            name="image",
            capacity=8,
            workers=1,
            runner=runner,
        )
        dispatcher.start()
        try:
            dispatcher.submit("image-1")
            self.assertTrue(first_started.wait(timeout=1))
            dispatcher.submit("image-2")
            self.assertEqual(dispatcher.snapshot()["running"], 1)
            self.assertEqual(dispatcher.snapshot()["waiting"], 1)
        finally:
            release.set()
            deadline = time.time() + 2
            while dispatcher.snapshot()["running"] and time.time() < deadline:
                time.sleep(0.01)
            dispatcher.stop()

        self.assertEqual(max_active, 1)


class LocalResultStoreTests(unittest.TestCase):
    def test_save_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary)
            store = LocalResultStore()
            fields = store.save(
                job_dir=job_dir,
                text="# OCR",
                inner_response={"text": "# OCR"},
            )
            metadata = {"storage": fields["storage"]}

            self.assertEqual(
                store.read_text(job_dir=job_dir, metadata=metadata),
                "# OCR",
            )
            self.assertTrue((job_dir / "inner_response.json").is_file())


class FakeOssObject:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeOssBucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.headers: dict[str, dict[str, str]] = {}

    def put_object_from_file(
        self,
        object_key: str,
        filename: str,
        *,
        headers: dict[str, str],
    ) -> None:
        self.objects[object_key] = Path(filename).read_bytes()
        self.headers[object_key] = headers

    def get_object(self, object_key: str) -> FakeOssObject:
        return FakeOssObject(self.objects[object_key])

    def sign_url(self, method: str, object_key: str, expires: int) -> str:
        return f"https://oss.example/{object_key}?method={method}&expires={expires}"


class OssResultStoreTests(unittest.TestCase):
    def test_uploads_markdown_response_and_inner_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary) / "document_20260804120000"
            image_file = job_dir / "artifacts" / "imgs" / "image_0.jpg"
            image_file.parent.mkdir(parents=True)
            image_file.write_bytes(b"image-bytes")
            bucket = FakeOssBucket()
            store = OssResultStore(
                bucket=bucket,
                bucket_name="ocr-bucket",
                prefix="glm_ocr_output",
                signed_url_expires_seconds=600,
            )

            fields = store.save(
                job_dir=job_dir,
                text="# OCR",
                inner_response={"text": "# OCR"},
            )
            metadata = {"storage": fields["storage"]}
            object_prefix = "glm_ocr_output/document_20260804120000"

            self.assertEqual(bucket.objects[f"{object_prefix}/result.md"], b"# OCR")
            self.assertEqual(
                bucket.objects[f"{object_prefix}/artifacts/imgs/image_0.jpg"],
                b"image-bytes",
            )
            self.assertEqual(
                store.read_text(job_dir=job_dir, metadata=metadata),
                "# OCR",
            )

            artifacts = store.public_artifacts(job_dir=job_dir, metadata=metadata)
            image = next(item for item in artifacts if item["type"] == "image")
            self.assertEqual(
                image["object_key"],
                f"{object_prefix}/artifacts/imgs/image_0.jpg",
            )
            self.assertIn("expires=600", image["url"])


if __name__ == "__main__":
    unittest.main()
