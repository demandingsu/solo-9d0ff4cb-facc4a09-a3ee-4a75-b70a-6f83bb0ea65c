"""Configuration for the plate publication service."""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def data_dir() -> Path:
    """Directory holding the SQLite database and uploaded artefacts."""
    return Path(_env("PLATE_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))


def database_path() -> Path:
    return Path(_env("PLATE_DB_PATH", str(data_dir() / "plate.sqlite3")))


def uploads_dir() -> Path:
    return Path(_env("PLATE_UPLOADS_DIR", str(data_dir() / "uploads")))


def max_upload_bytes() -> int:
    return int(_env("PLATE_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))


def quality_floor() -> float:
    """Registered lower bound for the positive control mean absorbance."""
    return float(_env("PLATE_QC_POSITIVE_MIN", "0.5"))
