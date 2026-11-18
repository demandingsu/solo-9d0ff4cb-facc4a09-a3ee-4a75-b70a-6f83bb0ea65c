"""SQLite-level guarantees that the database enforces, independent of app code."""

from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import LAYOUT_CSV, READINGS_CSV
from tests.test_lifecycle import new_plate, snapshot, upload


def _raw(db_file):
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_schema_blocks_second_active_publication(client, app_env):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    conn = _raw(app_env / "data" / "plate.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO publications
                    (plate_id, seq, map_version_id, reading_version_id, status,
                     is_active, trigger, qc_json, created_at)
                SELECT plate_id, 999, map_version_id, reading_version_id, 'published',
                       1, 'recompute', qc_json, created_at
                FROM publications WHERE plate_id = ?
                """,
                (plate_id,),
            )
    finally:
        conn.close()


def test_schema_blocks_unknown_publication_status(client, app_env):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    conn = _raw(app_env / "data" / "plate.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO publications
                    (plate_id, seq, map_version_id, reading_version_id, status,
                     is_active, trigger, qc_json, created_at)
                SELECT plate_id, 1000, map_version_id, reading_version_id, 'bogus',
                       0, 'recompute', qc_json, created_at
                FROM publications WHERE plate_id = ?
                """,
                (plate_id,),
            )
    finally:
        conn.close()


def test_input_versions_are_immutable_by_convention(client):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    before = snapshot(client, plate_id)
    # Re-uploading the identical reading produces a new version, never an edit.
    upload(client, plate_id, readings_text=READINGS_CSV)
    after = snapshot(client, plate_id)
    assert len(after["reading_versions"]) == len(before["reading_versions"]) + 1
    first = before["reading_versions"][0]
    same = next(v for v in after["reading_versions"] if v["id"] == first["id"])
    assert same["sha256"] == first["sha256"]
    # An identical re-upload still invalidates the old publication and republishes.
    assert after["active_publication"]["seq"] == 2
    assert after["publications"][1]["status"] == "superseded"


def test_stale_publication_keeps_result_and_qc(client):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    first = snapshot(client, plate_id)["active_publication"]
    upload(client, plate_id, readings_text=readings_override_small())
    stale = client.get(f"/api/plates/{plate_id}/publications/{first['id']}").json()["publication"]
    assert stale["status"] in {"superseded", "stale"}
    assert stale["result"]["samples"][0]["mean"] == pytest.approx(0.55)
    assert stale["qc"]["passed"] is True


def readings_override_small() -> str:
    return """well,absorbance
A1,0.100
A2,0.600
A3,0.700
A4,1.000
A5,1.100
A6,0.200
"""


def test_input_versions_are_immutable_at_sql_level(client, app_env):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    conn = _raw(app_env / "data" / "plate.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE reading_versions SET readings_json = '{}' WHERE plate_id = ?", (plate_id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM plate_map_versions WHERE plate_id = ?", (plate_id,))
    finally:
        conn.close()


def _chain_pair(client):
    from tests.test_chain import MULTI_HOP_OK, seed_plates, verify

    source, _, target = seed_plates(client)
    result = verify(client, source, target, MULTI_HOP_OK).json()["result"]
    return source, target, result["verification_id"]


def test_schema_blocks_second_active_verification(client, app_env):
    _, _, verification_id = _chain_pair(client)
    conn = _raw(app_env / "data" / "plate.sqlite3")
    try:
        source_plate_id, target_plate_id = conn.execute(
            "SELECT source_plate_id, target_plate_id FROM chain_verifications WHERE id = ?",
            (verification_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO chain_verifications
                    (source_plate_id, target_plate_id, seq, transfer_log_version_id,
                     source_map_version_id, target_map_version_id, status, is_active,
                     trigger, summary_json, created_at)
                SELECT source_plate_id, target_plate_id, 999, transfer_log_version_id,
                       source_map_version_id, target_map_version_id, 'completed', 1,
                       'recompute', summary_json, created_at
                FROM chain_verifications WHERE id = ?
                """,
                (verification_id,),
            )
    finally:
        conn.close()


def test_schema_blocks_unknown_verification_status(client, app_env):
    _, _, verification_id = _chain_pair(client)
    conn = _raw(app_env / "data" / "plate.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO chain_verifications
                    (source_plate_id, target_plate_id, seq, transfer_log_version_id,
                     source_map_version_id, target_map_version_id, status, is_active,
                     trigger, summary_json, created_at)
                SELECT source_plate_id, target_plate_id, 1000, transfer_log_version_id,
                       source_map_version_id, target_map_version_id, 'bogus', 0,
                       'recompute', summary_json, created_at
                FROM chain_verifications WHERE id = ?
                """,
                (verification_id,),
            )
    finally:
        conn.close()


def test_transfer_log_versions_are_immutable_at_sql_level(client, app_env):
    _, _, verification_id = _chain_pair(client)
    conn = _raw(app_env / "data" / "plate.sqlite3")
    try:
        log_id = conn.execute(
            "SELECT transfer_log_version_id FROM chain_verifications WHERE id = ?",
            (verification_id,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE transfer_log_versions SET operations_json = '[]' WHERE id = ?",
                (log_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM transfer_log_versions WHERE id = ?", (log_id,))
    finally:
        conn.close()
