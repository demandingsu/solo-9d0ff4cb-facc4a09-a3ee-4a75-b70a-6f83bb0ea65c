"""Additional end-to-end edge cases."""

from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import LAYOUT_CSV, READINGS_CSV, readings_override
from tests.test_lifecycle import new_plate, snapshot, upload


def test_grid_inputs_publish(client):
    plate_id = new_plate(client)
    readings_grid = ",1,2,3,4,5\nA,0.1,0.6,0.7,1.0,1.1\nB,1.5,,,,,\n"
    response = upload(
        client,
        plate_id,
        map_text=",1,2,3,4,5\nA,BLANK,S1,S1,S2,S2\nB,PC,,,,\n",
        readings_text=readings_grid,
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["outcome"] == "published"
    active = snapshot(client, plate_id)["active_publication"]
    assert active["inputs"]["map"]["layout"]["format"] == "96-well"
    assert active["result"]["positive_control"]["mean"] == 1.4


def test_upload_size_limit(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "max_upload_bytes", lambda: 10)
    plate_id = new_plate(client)
    response = upload(client, plate_id, readings_text=READINGS_CSV)
    assert response.status_code == 400
    assert "上限" in response.json()["error"]


def test_binary_upload_rejected(client):
    plate_id = new_plate(client)
    response = client.post(
        f"/api/plates/{plate_id}/uploads",
        files={"readings_file": ("r.csv", b"\x00\x01\x02binary", "text/csv")},
    )
    assert response.status_code == 400


def test_semicolon_and_chinese_headers(client):
    plate_id = new_plate(client)
    layout = "孔位;角色;样本\nA1;空白;\nA2;样本;X\nA3;样本;X\nA4;阳性对照;\n"
    readings = "孔位;吸光度\nA1;0,1\nA2;0,6\nA3;0,7\nA4;1,2\n"
    response = upload(client, plate_id, map_text=layout, readings_text=readings)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["outcome"] == "published"
    active = snapshot(client, plate_id)["active_publication"]
    assert active["result"]["samples"][0]["mean"] == 0.55


def test_duplicate_active_index_guard(client, app_env):
    """The schema itself forbids two active publications for one plate."""
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    conn = sqlite3.connect(str(app_env / "data" / "plate.sqlite3"))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO publications (plate_id, seq, map_version_id, reading_version_id, "
                "status, is_active, trigger, qc_json, created_at) "
                "SELECT 1, 99, map_version_id, reading_version_id, 'published', 1, 'recompute', "
                "qc_json, created_at FROM publications WHERE plate_id = ?",
                (plate_id,),
            )
    finally:
        conn.close()


def test_stale_reason_mentions_failed_qc(client):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    response = upload(client, plate_id, readings_text=readings_override(A6=0.2))
    assert response.json()["result"]["outcome"] == "failed"
    active = snapshot(client, plate_id)["active_publication"]
    assert "孔位读数已更新至 v2" in active["stale_reason"]
    assert "阳性对照" in active["stale_reason"]
