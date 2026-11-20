#!/usr/bin/env python3
"""V5 entry point using the queue service with protected math translation."""

from __future__ import annotations

import sys
from pathlib import Path

V3_SERVICE_DIR = Path(__file__).resolve().parents[1] / "glm_ocr_v3"
sys.path.insert(0, str(V3_SERVICE_DIR))

from service import main  # noqa: E402


if __name__ == "__main__":
    main()
