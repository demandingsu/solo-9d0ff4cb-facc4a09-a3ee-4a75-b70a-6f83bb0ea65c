"""Shared pytest fixtures with an isolated data directory per test."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PLATE_DB_PATH", str(tmp_path / "data" / "plate.sqlite3"))
    monkeypatch.setenv("PLATE_UPLOADS_DIR", str(tmp_path / "data" / "uploads"))
    monkeypatch.setenv("PLATE_QC_POSITIVE_MIN", "0.5")
    for module in [name for name in sys.modules if name == "app" or name.startswith("app.")]:
        del sys.modules[module]
    from app import db

    db.init_db()
    yield tmp_path
    for module in [name for name in sys.modules if name == "app" or name.startswith("app.")]:
        del sys.modules[module]


@pytest.fixture()
def client(app_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


LAYOUT_CSV = """well,role,sample_id
A1,blank,
A2,sample,S1
A3,sample,S1
A4,sample,S2
A5,sample,S2
A6,positive_control,
"""

READINGS_CSV = """well,absorbance
A1,0.100
A2,0.600
A3,0.700
A4,1.000
A5,1.100
A6,1.200
"""


def layout_with_positive(value_well: str = "A6", role: str = "positive_control") -> str:
    return LAYOUT_CSV


def readings_override(**values: float) -> str:
    base = {
        "A1": 0.100,
        "A2": 0.600,
        "A3": 0.700,
        "A4": 1.000,
        "A5": 1.100,
        "A6": 1.200,
    }
    base.update({key.upper(): value for key, value in values.items()})
    lines = ["well,absorbance"] + [f"{well},{value:.6f}" for well, value in base.items()]
    return "\n".join(lines) + "\n"


def upload(client, plate_id: int, *, map_text: str | None = None, readings_text: str | None = None, note: str = ""):
    files = {}
    data = {"note": note}
    if map_text is not None:
        files["map_file"] = ("layout.csv", map_text.encode("utf-8"), "text/csv")
    if readings_text is not None:
        files["readings_file"] = ("readings.csv", readings_text.encode("utf-8"), "text/csv")
    return client.post(f"/api/plates/{plate_id}/uploads", data=data, files=files)
