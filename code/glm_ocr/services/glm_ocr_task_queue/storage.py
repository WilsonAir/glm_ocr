"""Result storage boundary for local files now and OBS later."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class ResultStore(Protocol):
    def save(
        self,
        *,
        job_dir: Path,
        text: str,
        inner_response: dict[str, Any],
    ) -> dict[str, Any]: ...

    def read_text(self, *, job_dir: Path, metadata: dict[str, Any]) -> str: ...


class LocalResultStore:
    """Persist canonical output locally behind the same interface OBS will use."""

    def save(
        self,
        *,
        job_dir: Path,
        text: str,
        inner_response: dict[str, Any],
    ) -> dict[str, Any]:
        result_file = job_dir / "result.md"
        response_file = job_dir / "inner_response.json"
        result_file.write_text(text, encoding="utf-8")
        response_file.write_text(
            json.dumps(inner_response, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "storage": {
                "type": "local",
                "result_file": result_file.name,
                "inner_response_file": response_file.name,
            }
        }

    def read_text(self, *, job_dir: Path, metadata: dict[str, Any]) -> str:
        storage = metadata.get("storage", {})
        relative = storage.get("result_file", "result.md")
        result_file = (job_dir / str(relative)).resolve()
        if not result_file.is_relative_to(job_dir.resolve()) or not result_file.is_file():
            raise FileNotFoundError(relative)
        return result_file.read_text(encoding="utf-8")


def build_result_store(config: dict[str, Any]) -> ResultStore:
    storage_type = str(config.get("type", "local"))
    if storage_type == "local":
        return LocalResultStore()
    raise ValueError(
        f"Unsupported storage type {storage_type!r}; "
        "add an adapter implementing ResultStore"
    )
