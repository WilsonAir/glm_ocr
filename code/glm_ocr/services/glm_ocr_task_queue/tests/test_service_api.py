from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.glm_ocr_task_queue import service


class FakeResponse:
    def __init__(self, text: str = "# OCR result"):
        self._text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "job_id": "inner-job",
            "result_url": "/results/inner-job",
            "text": self._text,
        }


class TaskServiceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        config = root / "config.yaml"
        config.write_text(
            "\n".join([
                "service:",
                "  host: 127.0.0.1",
                "  port: 18092",
                f"  output_root: {root / 'tasks'}",
                "inner_service:",
                "  base_url: http://inner.test",
                "  timeout: 10",
                "queues:",
                "  pdf:",
                "    workers: 2",
                "    capacity: 4",
                "  image:",
                "    workers: 1",
                "    capacity: 8",
                "storage:",
                "  type: local",
            ]),
            encoding="utf-8",
        )
        service.configure(config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def wait_for_status(
        client: TestClient,
        job_id: str,
        expected: set[str],
        timeout: float = 2,
    ) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            response = client.get(f"/tasks/{job_id}/status")
            metadata = response.json()
            if metadata["status"] in expected:
                return metadata
            time.sleep(0.01)
        raise AssertionError(f"Task did not reach {expected}")

    def test_submit_status_and_result(self) -> None:
        with patch.object(
            service.requests,
            "post",
            return_value=FakeResponse(),
        ):
            with TestClient(service.app) as client:
                response = client.post(
                    "/tasks",
                    files={
                        "file": (
                            "patient 001.pdf",
                            b"%PDF-1.7 test",
                            "application/pdf",
                        )
                    },
                )
                self.assertEqual(response.status_code, 202)
                submitted = response.json()
                self.assertEqual(submitted["name"], "patient 001")
                self.assertTrue(submitted["job_id"].startswith("patient_001_"))

                completed = self.wait_for_status(
                    client,
                    submitted["job_id"],
                    {"completed"},
                )
                self.assertEqual(completed["inner_job_id"], "inner-job")

                result = client.get(submitted["result_url"])
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["text"], "# OCR result")

    def test_cancel_running_task(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_post(*_: object, **__: object) -> FakeResponse:
            entered.set()
            release.wait(timeout=2)
            return FakeResponse()

        with patch.object(service.requests, "post", side_effect=blocking_post):
            with TestClient(service.app) as client:
                response = client.post(
                    "/tasks?image_mode=model_only",
                    files={"file": ("crop.png", b"image", "image/png")},
                )
                job_id = response.json()["job_id"]
                self.assertTrue(entered.wait(timeout=1))
                self.wait_for_status(client, job_id, {"running"})

                canceled = client.delete(f"/tasks/{job_id}")
                self.assertEqual(canceled.status_code, 202)
                self.assertEqual(
                    canceled.json()["status"],
                    "cancel_requested",
                )

                release.set()
                self.wait_for_status(client, job_id, {"canceled"})
                result = client.get(f"/tasks/{job_id}/result")
                self.assertEqual(result.status_code, 409)

    def test_full_model_queue_keeps_accepting_into_persistent_backlog(
        self,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        active = 0

        def blocking_post(*_: object, **__: object) -> FakeResponse:
            nonlocal active
            with lock:
                active += 1
                if active >= 2:
                    entered.set()
            release.wait(timeout=3)
            return FakeResponse()

        with patch.object(service.requests, "post", side_effect=blocking_post):
            with TestClient(service.app) as client:
                job_ids = []
                for index in range(7):
                    response = client.post(
                        "/tasks",
                        files={
                            "file": (
                                f"burst-{index}.pdf",
                                b"%PDF-1.7 test",
                                "application/pdf",
                            )
                        },
                    )
                    self.assertEqual(response.status_code, 202)
                    job_ids.append(response.json()["job_id"])

                self.assertTrue(entered.wait(timeout=1))
                deadline = time.time() + 2
                snapshot = {}
                while time.time() < deadline:
                    snapshot = client.get("/health").json()["queues"]["pdf"]
                    if (
                        snapshot["running"] == 2
                        and snapshot["waiting"] == 4
                        and snapshot["backlog"] == 1
                    ):
                        break
                    time.sleep(0.01)

                self.assertEqual(snapshot["running"], 2)
                self.assertEqual(snapshot["waiting"], 4)
                self.assertEqual(snapshot["backlog"], 1)

                statuses = [
                    client.get(f"/tasks/{job_id}/status").json()["status"]
                    for job_id in job_ids
                ]
                self.assertEqual(statuses.count("running"), 2)
                self.assertEqual(statuses.count("queued"), 4)
                self.assertEqual(statuses.count("pending"), 1)

                pending_id = job_ids[statuses.index("pending")]
                canceled = client.delete(f"/tasks/{pending_id}")
                self.assertEqual(canceled.status_code, 200)
                self.assertEqual(canceled.json()["status"], "canceled")

                release.set()

    def test_queued_task_is_recovered_from_disk(self) -> None:
        repository, _ = service._services()
        job_id, job_dir = repository.create("recovery")
        input_file = Path("input") / "document.pdf"
        (job_dir / input_file).parent.mkdir(parents=True)
        (job_dir / input_file).write_bytes(b"%PDF-1.7 recovery")
        repository.write(job_id, {
            "job_id": job_id,
            "name": "recovery",
            "status": "queued",
            "queue": "pdf",
            "filename": "document.pdf",
            "input_file": str(input_file),
            "content_type": "pdf",
            "image_mode": "auto",
            "created_at": "2026-07-31T15:00:00+08:00",
        })

        with patch.object(
            service.requests,
            "post",
            return_value=FakeResponse("# recovered"),
        ):
            with TestClient(service.app) as client:
                completed = self.wait_for_status(
                    client,
                    job_id,
                    {"completed"},
                )

        self.assertEqual(completed["recovery_count"], 1)
        self.assertEqual(
            (job_dir / "result.md").read_text(encoding="utf-8"),
            "# recovered",
        )


if __name__ == "__main__":
    unittest.main()
