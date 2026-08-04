"""Result storage boundary for local files and Alibaba Cloud OSS."""

from __future__ import annotations

import json
import mimetypes
import os
import re
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

    def public_artifacts(
        self,
        *,
        job_dir: Path,
        metadata: dict[str, Any],
    ) -> list[Any]: ...


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

    def public_artifacts(
        self,
        *,
        job_dir: Path,
        metadata: dict[str, Any],
    ) -> list[str]:
        return sorted(
            str(path.relative_to(job_dir))
            for path in job_dir.rglob("*")
            if path.is_file() and path.name != ".job.json.tmp"
        )


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}


def load_environment_file(path: Path) -> None:
    """Load simple KEY=VALUE lines without overriding real process variables."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _content_type(path: Path) -> str:
    if path.suffix.lower() == ".md":
        return "text/markdown; charset=utf-8"
    if path.suffix.lower() == ".json":
        return "application/json; charset=utf-8"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _artifact_kind(path: Path) -> str:
    if path.suffix.lower() in _IMAGE_SUFFIXES:
        return "image"
    if path.suffix.lower() == ".md":
        return "markdown"
    if path.suffix.lower() == ".json":
        return "json"
    return "file"


class OssResultStore(LocalResultStore):
    """Keep a local task copy and upload the task outputs to Alibaba Cloud OSS."""

    def __init__(
        self,
        *,
        bucket: Any,
        bucket_name: str,
        prefix: str,
        signed_url_expires_seconds: int,
    ) -> None:
        self._bucket = bucket
        self._bucket_name = bucket_name
        self._prefix = prefix.strip("/")
        self._signed_url_expires_seconds = signed_url_expires_seconds

    def _object_key(self, job_dir: Path, relative_path: str) -> str:
        parts = [part for part in (self._prefix, job_dir.name, relative_path) if part]
        return "/".join(parts)

    def _output_files(self, job_dir: Path) -> list[tuple[Path, str]]:
        files = [
            (job_dir / "result.md", "result.md"),
            (job_dir / "inner_response.json", "inner_response.json"),
        ]
        artifacts_root = job_dir / "artifacts"
        if artifacts_root.is_dir():
            files.extend(
                (
                    path,
                    f"artifacts/{path.relative_to(artifacts_root).as_posix()}",
                )
                for path in sorted(artifacts_root.rglob("*"))
                if path.is_file()
            )
        return files

    def save(
        self,
        *,
        job_dir: Path,
        text: str,
        inner_response: dict[str, Any],
    ) -> dict[str, Any]:
        super().save(job_dir=job_dir, text=text, inner_response=inner_response)
        manifest: list[dict[str, str]] = []
        for source, relative_path in self._output_files(job_dir):
            object_key = self._object_key(job_dir, relative_path)
            self._bucket.put_object_from_file(
                object_key,
                str(source),
                headers={"Content-Type": _content_type(source)},
            )
            manifest.append(
                {
                    "name": source.name,
                    "path": relative_path,
                    "type": _artifact_kind(source),
                    "object_key": object_key,
                }
            )

        return {
            "storage": {
                "type": "oss",
                "bucket": self._bucket_name,
                "prefix": self._object_key(job_dir, ""),
                "result_object_key": self._object_key(job_dir, "result.md"),
                "artifacts": manifest,
            }
        }

    def read_text(self, *, job_dir: Path, metadata: dict[str, Any]) -> str:
        storage = metadata.get("storage", {})
        object_key = storage.get("result_object_key")
        if not isinstance(object_key, str) or not object_key:
            raise FileNotFoundError("OSS result object key is missing")
        response = self._bucket.get_object(object_key)
        return response.read().decode("utf-8")

    def public_artifacts(
        self,
        *,
        job_dir: Path,
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        storage = metadata.get("storage", {})
        raw_artifacts = storage.get("artifacts", [])
        if not isinstance(raw_artifacts, list):
            return []
        artifacts: list[dict[str, Any]] = []
        for raw in raw_artifacts:
            if not isinstance(raw, dict):
                continue
            object_key = raw.get("object_key")
            if not isinstance(object_key, str) or not object_key:
                continue
            artifact = dict(raw)
            artifact["url"] = self._bucket.sign_url(
                "GET",
                object_key,
                self._signed_url_expires_seconds,
            )
            artifact["url_expires_in_seconds"] = self._signed_url_expires_seconds
            artifacts.append(artifact)
        return artifacts


def build_result_store(
    config: dict[str, Any],
    *,
    env_file: Path | None = None,
) -> ResultStore:
    storage_type = str(config.get("type", "local"))
    if storage_type == "local":
        return LocalResultStore()
    if storage_type != "oss":
        raise ValueError(f"Unsupported storage type {storage_type!r}")

    if env_file is not None:
        load_environment_file(env_file)
    required_names = (
        "OSS_ENDPOINT",
        "OSS_ACCESS_KEY_ID",
        "OSS_ACCESS_KEY_SECRET",
        "OSS_BUCKET_NAME",
    )
    missing = [name for name in required_names if not os.environ.get(name)]
    if missing:
        raise ValueError(
            "OSS configuration is missing environment variables: " + ", ".join(missing)
        )
    try:
        signed_url_expires_seconds = int(
            os.environ.get(
                "OSS_SIGNED_URL_EXPIRES_SECONDS",
                str(config.get("signed_url_expires_seconds", 3600)),
            )
        )
    except ValueError as exc:
        raise ValueError("OSS_SIGNED_URL_EXPIRES_SECONDS must be an integer") from exc
    if signed_url_expires_seconds <= 0:
        raise ValueError("OSS signed URL expiry must be greater than zero")

    try:
        import oss2
    except ImportError as exc:
        raise RuntimeError(
            "Alibaba Cloud OSS support requires the 'oss2' package; "
            "install code/glm_ocr/requirements.txt"
        ) from exc

    endpoint = os.environ["OSS_ENDPOINT"]
    bucket_name = os.environ["OSS_BUCKET_NAME"]
    prefix = os.environ.get("OSS_PREFIX", "glm_ocr_output")
    auth = oss2.Auth(
        os.environ["OSS_ACCESS_KEY_ID"],
        os.environ["OSS_ACCESS_KEY_SECRET"],
    )
    return OssResultStore(
        bucket=oss2.Bucket(auth, endpoint, bucket_name),
        bucket_name=bucket_name,
        prefix=prefix,
        signed_url_expires_seconds=signed_url_expires_seconds,
    )
