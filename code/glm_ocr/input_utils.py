"""Input file type detection for GLM-OCR SDK scripts."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

# Keep in sync with glmocr.cli._SUPPORTED_SUFFIXES
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = IMAGE_SUFFIXES | PDF_SUFFIXES


class InputType(str, Enum):
    PDF = "pdf"
    IMAGE = "image"


def detect_input_type(path: Path) -> InputType:
    """Detect whether *path* is a PDF or image input."""
    suffix = path.suffix.lower()
    if suffix in PDF_SUFFIXES:
        return InputType.PDF
    if suffix in IMAGE_SUFFIXES:
        return InputType.IMAGE
    supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
    raise ValueError(
        f"Unsupported input type {suffix!r} for {path}. "
        f"Supported suffixes: {supported}"
    )


def validate_input(path: Path) -> tuple[Path, InputType]:
    """Resolve *path* and validate that it is a supported PDF or image file."""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Input file not found: {path}")
    input_type = detect_input_type(resolved)
    return resolved, input_type
