"""The shipped sample files must import successfully."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_lifecycle import new_plate, snapshot, upload

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def test_sample_layout_and_readings_publish(client):
    plate_id = new_plate(client)
    response = upload(
        client,
        plate_id,
        map_text=(SAMPLES / "layout.csv").read_text(encoding="utf-8"),
        readings_text=(SAMPLES / "readings.csv").read_text(encoding="utf-8"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["outcome"] == "published"
    active = snapshot(client, plate_id)["active_publication"]
    means = {s["sample_id"]: s["mean"] for s in active["result"]["samples"]}
    blank = active["result"]["blank"]["mean"]
    assert blank == pytest.approx(0.05)
    assert means["S-001"] == pytest.approx(((0.612 - blank) + (0.598 - blank)) / 2)
    assert set(means) == {"S-001", "S-002", "S-003"}
