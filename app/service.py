"""Application service: versioned inputs, publication lifecycle, read models.

Lifecycle
---------
* Uploading a plate map or a reading file always creates a new immutable
  version.  In the same write transaction the currently active publication is
  marked ``stale`` (it stays active and viewable with the exact inputs it was
  computed from).
* A recalculation then runs against one consistent snapshot of the newest map
  version and newest reading version.  On success a new publication becomes
  active and the previous one is marked ``superseded``.  On failure a
  ``failed`` attempt is recorded and the stale publication stays active.
* Every write uses ``BEGIN IMMEDIATE``; every read model is loaded inside one
  read transaction.  A publication stores concrete map/reading version ids, so
  a report can never mix inputs from different versions.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, db, parsing, qc

STATUS_PUBLISHED = "published"
STATUS_STALE = "stale"
STATUS_SUPERSEDED = "superseded"
STATUS_FAILED = "failed"

TRIGGER_UPLOAD = "upload"
TRIGGER_RECOMPUTE = "recompute"


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None) -> Any:
    return json.loads(value) if value else None


# ---------------------------------------------------------------------------
# Plates


def _plate_row(conn: sqlite3.Connection, plate_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM plates WHERE id = ?", (plate_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"板 {plate_id} 不存在")
    return row


def create_plate(name: str) -> dict[str, Any]:
    clean = " ".join(name.split())
    if not clean:
        raise parsing.InputError("板名称不能为空")
    if len(clean) > 120:
        raise parsing.InputError("板名称不能超过 120 个字符")
    conn = db.connect()
    try:
        with db.transaction(conn, immediate=True):
            try:
                cursor = conn.execute(
                    "INSERT INTO plates (name, created_at) VALUES (?, ?)",
                    (clean, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"板名称“{clean}”已存在") from exc
            plate_id = int(cursor.lastrowid)
            row = _plate_row(conn, plate_id)
            return dict(row)
    finally:
        conn.close()


def list_plates() -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM plate_map_versions m WHERE m.plate_id = p.id) AS map_version_count,
                   (SELECT COUNT(*) FROM reading_versions r WHERE r.plate_id = p.id) AS reading_version_count,
                   pub.id AS active_publication_id,
                   pub.seq AS active_publication_seq,
                   pub.status AS active_publication_status,
                   pub.published_at AS active_published_at,
                   pub.stale_at AS active_stale_at
            FROM plates p
            LEFT JOIN publications pub ON pub.plate_id = p.id AND pub.is_active = 1
            ORDER BY p.id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Uploads / versions


def _safe_original_name(name: str | None) -> str:
    if not name:
        return "upload"
    base = Path(name.replace("\\", "/")).name.strip()
    return (base or "upload")[:255]


def _store_file(plate_id: int, data: bytes, filename: str) -> tuple[str, str, int, str]:
    digest = hashlib.sha256(data).hexdigest()
    suffix = Path(filename).suffix.lower()
    if not suffix or len(suffix) > 12 or not suffix[1:].isalnum():
        suffix = ".txt"
    directory = config.uploads_dir() / str(plate_id)
    directory.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    target = directory / stored_name
    temp = directory / f".{stored_name}.tmp"
    temp.write_bytes(data)
    os.replace(temp, target)
    return stored_name, f"plate-{plate_id}/{stored_name}", len(data), digest


def _remove_file(stored_name: str | None, plate_id: int) -> None:
    if not stored_name:
        return
    try:
        (config.uploads_dir() / str(plate_id) / stored_name).unlink()
    except FileNotFoundError:
        pass


def resolve_stored_file(stored_name: str, plate_id: int) -> Path:
    base = (config.uploads_dir() / str(plate_id)).resolve()
    target = (base / stored_name).resolve()
    if base != target.parent or not target.is_file():
        raise NotFoundError("上传文件不存在")
    return target


def _latest_version(
    conn: sqlite3.Connection, table: str, plate_id: int, *, required: bool
) -> sqlite3.Row | None:
    if table not in {"plate_map_versions", "reading_versions"}:
        raise ValueError("unsupported table")
    row = conn.execute(
        f"SELECT * FROM {table} WHERE plate_id = ? ORDER BY seq DESC LIMIT 1",
        (plate_id,),
    ).fetchone()
    if row is None and required:
        kind = "孔位图" if table == "plate_map_versions" else "孔位读数"
        raise ConflictError(f"尚未上传{kind}")
    return row


def _active_publication(conn: sqlite3.Connection, plate_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM publications WHERE plate_id = ? AND is_active = 1",
        (plate_id,),
    ).fetchone()


def _mark_active_stale(conn: sqlite3.Connection, plate_id: int, reason: str) -> int | None:
    """Invalidate the active publication; it stays viewable with its inputs."""
    active = _active_publication(conn, plate_id)
    if active is None:
        return None
    if active["status"] == STATUS_PUBLISHED:
        conn.execute(
            "UPDATE publications SET status = ?, stale_at = ?, stale_reason = ? WHERE id = ?",
            (STATUS_STALE, utcnow(), reason, active["id"]),
        )
        return int(active["id"])
    if active["status"] == STATUS_STALE and reason:
        conn.execute(
            "UPDATE publications SET stale_reason = ? WHERE id = ?",
            (reason, active["id"]),
        )
    return None


def _publish(
    conn: sqlite3.Connection,
    plate_id: int,
    *,
    map_row: sqlite3.Row,
    reading_row: sqlite3.Row,
    trigger: str,
    stale_reasons: list[str] | None = None,
) -> dict[str, Any]:
    layout = _loads(map_row["layout_json"])
    readings = _loads(reading_row["readings_json"])
    floor = config.quality_floor()
    evaluation = qc.evaluate(layout, readings, floor)
    now = utcnow()
    seq = db.next_seq(conn, "publications", plate_id)
    previous = _active_publication(conn, plate_id)

    if evaluation["passed"]:
        if previous is not None:
            conn.execute(
                "UPDATE publications SET status = ?, is_active = 0, superseded_by = ? WHERE id = ?",
                (STATUS_SUPERSEDED, None, previous["id"]),
            )
        cursor = conn.execute(
            """
            INSERT INTO publications
                (plate_id, seq, map_version_id, reading_version_id, status, is_active,
                 trigger, result_json, qc_json, error, created_at, published_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, ?, ?)
            """,
            (
                plate_id,
                seq,
                map_row["id"],
                reading_row["id"],
                STATUS_PUBLISHED,
                trigger,
                _dumps(evaluation["result"]),
                _dumps(evaluation["qc"]),
                now,
                now,
            ),
        )
        new_id = int(cursor.lastrowid)
        if previous is not None:
            conn.execute(
                "UPDATE publications SET superseded_by = ? WHERE id = ?",
                (new_id, previous["id"]),
            )
        return {
            "outcome": "published",
            "publication_id": new_id,
            "publication_seq": seq,
            "superseded_publication_id": int(previous["id"]) if previous else None,
            "qc": evaluation["qc"],
        }

    error = "；".join(evaluation["qc"]["issues"]) or "质控未通过"
    # A failed attempt never replaces the active publication.  If inputs had
    # changed, that publication is already stale and stays active (viewable
    # with its original inputs); a still-valid publication is left untouched.
    if previous is not None and previous["status"] == STATUS_STALE and stale_reasons:
        conn.execute(
            "UPDATE publications SET stale_reason = ? WHERE id = ?",
            ("；".join(stale_reasons) + f"；重算未通过质控：{error}", previous["id"]),
        )
    cursor = conn.execute(
        """
        INSERT INTO publications
            (plate_id, seq, map_version_id, reading_version_id, status, is_active,
             trigger, result_json, qc_json, error, created_at)
        VALUES (?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, ?)
        """,
        (
            plate_id,
            seq,
            map_row["id"],
            reading_row["id"],
            STATUS_FAILED,
            trigger,
            _dumps(evaluation["qc"]),
            error,
            now,
        ),
    )
    failed_id = int(cursor.lastrowid)
    return {
        "outcome": "failed",
        "publication_id": int(previous["id"]) if previous else None,
        "publication_seq": int(previous["seq"]) if previous else None,
        "failed_attempt_id": failed_id,
        "failed_attempt_seq": seq,
        "active_is_stale": previous is not None and previous["status"] == STATUS_STALE,
        "qc": evaluation["qc"],
        "error": error,
    }


def ingest(
    plate_id: int,
    *,
    map_data: bytes | None = None,
    map_filename: str | None = None,
    readings_data: bytes | None = None,
    readings_filename: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Store new input version(s) and attempt one consistent recalculation."""
    if map_data is None and readings_data is None:
        raise parsing.InputError("请至少上传孔位图或孔位读数文件")

    clean_note = (note or "").strip() or None
    if clean_note and len(clean_note) > 500:
        raise parsing.InputError("备注不能超过 500 个字符")

    # Parse before touching the database so malformed files create no version.
    map_parsed = parsing.parse_layout(map_data) if map_data is not None else None
    readings_parsed = parsing.parse_readings(readings_data) if readings_data is not None else None

    conn = db.connect()
    stored: list[str] = []
    try:
        with db.transaction(conn, immediate=True):
            _plate_row(conn, plate_id)
            now = utcnow()
            stale_reasons: list[str] = []
            new_map_label = new_reading_label = None

            if map_parsed is not None and map_data is not None:
                filename = _safe_original_name(map_filename)
                seq = db.next_seq(conn, "plate_map_versions", plate_id)
                stored_name, _, size, digest = _store_file(plate_id, map_data, filename)
                stored.append(stored_name)
                conn.execute(
                    """
                    INSERT INTO plate_map_versions
                        (plate_id, seq, source, note, original_filename, stored_name,
                         media_type, sha256, byte_size, layout_json, created_at)
                    VALUES (?, ?, 'upload', ?, ?, ?, 'text/csv', ?, ?, ?, ?)
                    """,
                    (
                        plate_id,
                        seq,
                        clean_note,
                        filename,
                        stored_name,
                        digest,
                        size,
                        _dumps(map_parsed.layout),
                        now,
                    ),
                )
                new_map_label = seq
                stale_reasons.append(f"孔位图已更新至 v{seq}")

            if readings_parsed is not None and readings_data is not None:
                filename = _safe_original_name(readings_filename)
                seq = db.next_seq(conn, "reading_versions", plate_id)
                stored_name, _, size, digest = _store_file(plate_id, readings_data, filename)
                stored.append(stored_name)
                conn.execute(
                    """
                    INSERT INTO reading_versions
                        (plate_id, seq, source, note, original_filename, stored_name,
                         media_type, sha256, byte_size, readings_json, warnings_json, created_at)
                    VALUES (?, ?, 'upload', ?, ?, ?, 'text/csv', ?, ?, ?, ?, ?)
                    """,
                    (
                        plate_id,
                        seq,
                        clean_note,
                        filename,
                        stored_name,
                        digest,
                        size,
                        _dumps(readings_parsed.readings),
                        _dumps(readings_parsed.warnings),
                        now,
                    ),
                )
                new_reading_label = seq
                stale_reasons.append(f"孔位读数已更新至 v{seq}")

            marked = _mark_active_stale(conn, plate_id, "；".join(stale_reasons))

            map_row = _latest_version(conn, "plate_map_versions", plate_id, required=False)
            reading_row = _latest_version(conn, "reading_versions", plate_id, required=False)
            if map_row is None or reading_row is None:
                missing = "孔位图" if map_row is None else "孔位读数"
                return {
                    "outcome": "inputs_incomplete",
                    "message": f"已保存新版本，但尚未上传{missing}，暂不能计算。",
                    "stale_publication_id": marked,
                    "new_map_version": new_map_label,
                    "new_reading_version": new_reading_label,
                    "qc": None,
                }

            result = _publish(
                conn,
                plate_id,
                map_row=map_row,
                reading_row=reading_row,
                trigger=TRIGGER_UPLOAD,
                stale_reasons=stale_reasons,
            )
            result.update(
                {
                    "stale_publication_id": marked,
                    "new_map_version": new_map_label,
                    "new_reading_version": new_reading_label,
                    "map_version_seq": int(map_row["seq"]),
                    "reading_version_seq": int(reading_row["seq"]),
                }
            )
            return result
    except BaseException:
        for stored_name in stored:
            _remove_file(stored_name, plate_id)
        raise
    finally:
        conn.close()


def recompute(plate_id: int) -> dict[str, Any]:
    """Retry publication from the newest stored versions."""
    conn = db.connect()
    try:
        with db.transaction(conn, immediate=True):
            _plate_row(conn, plate_id)
            map_row = _latest_version(conn, "plate_map_versions", plate_id, required=True)
            reading_row = _latest_version(conn, "reading_versions", plate_id, required=True)
            active = _active_publication(conn, plate_id)
            if (
                active is not None
                and active["status"] == STATUS_PUBLISHED
                and active["map_version_id"] == map_row["id"]
                and active["reading_version_id"] == reading_row["id"]
            ):
                return {
                    "outcome": "unchanged",
                    "publication_id": int(active["id"]),
                    "publication_seq": int(active["seq"]),
                    "message": "输入未变化，当前发布版仍然有效。",
                }
            return _publish(
                conn,
                plate_id,
                map_row=map_row,
                reading_row=reading_row,
                trigger=TRIGGER_RECOMPUTE,
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read models


def _publication_view(
    conn: sqlite3.Connection,
    pub: sqlite3.Row,
    *,
    plate_id: int,
    latest_map_id: int | None,
    latest_reading_id: int | None,
) -> dict[str, Any]:
    map_row = conn.execute(
        "SELECT * FROM plate_map_versions WHERE id = ? AND plate_id = ?",
        (pub["map_version_id"], plate_id),
    ).fetchone()
    reading_row = conn.execute(
        "SELECT * FROM reading_versions WHERE id = ? AND plate_id = ?",
        (pub["reading_version_id"], plate_id),
    ).fetchone()
    superseded_by = None
    if pub["superseded_by"]:
        row = conn.execute(
            "SELECT id, seq FROM publications WHERE id = ? AND plate_id = ?",
            (pub["superseded_by"], plate_id),
        ).fetchone()
        if row is not None:
            superseded_by = {"id": int(row["id"]), "seq": int(row["seq"])}
    return {
        "id": int(pub["id"]),
        "seq": int(pub["seq"]),
        "status": pub["status"],
        "active": bool(pub["is_active"]),
        "trigger": pub["trigger"],
        "created_at": pub["created_at"],
        "published_at": pub["published_at"],
        "stale_at": pub["stale_at"],
        "stale_reason": pub["stale_reason"],
        "superseded_by": superseded_by,
        "error": pub["error"],
        "qc": _loads(pub["qc_json"]),
        "result": _loads(pub["result_json"]),
        "inputs": {
            "map": _version_summary(map_row, "map"),
            "reading": _version_summary(reading_row, "reading"),
        },
        "inputs_are_latest": (
            latest_map_id is not None
            and latest_reading_id is not None
            and pub["map_version_id"] == latest_map_id
            and pub["reading_version_id"] == latest_reading_id
        ),
    }


def _version_summary(row: sqlite3.Row | None, kind: str) -> dict[str, Any] | None:
    if row is None:
        return None
    summary = {
        "id": int(row["id"]),
        "seq": int(row["seq"]),
        "kind": kind,
        "source": row["source"],
        "note": row["note"],
        "original_filename": row["original_filename"],
        "sha256": row["sha256"],
        "byte_size": int(row["byte_size"]),
        "created_at": row["created_at"],
    }
    if kind == "map":
        summary["layout"] = _loads(row["layout_json"])
    else:
        summary["warnings"] = _loads(row["warnings_json"]) or []
        summary["well_count"] = (_loads(row["readings_json"]) or {}).get("well_count", 0)
    return summary


def get_plate_snapshot(plate_id: int) -> dict[str, Any]:
    conn = db.connect()
    try:
        with db.transaction(conn, immediate=False):
            plate = dict(_plate_row(conn, plate_id))
            latest_map = _latest_version(conn, "plate_map_versions", plate_id, required=False)
            latest_reading = _latest_version(conn, "reading_versions", plate_id, required=False)
            pub_rows = conn.execute(
                "SELECT * FROM publications WHERE plate_id = ? ORDER BY seq DESC",
                (plate_id,),
            ).fetchall()
            active = next((row for row in pub_rows if row["is_active"]), None)
            active_view = (
                _publication_view(
                    conn,
                    active,
                    plate_id=plate_id,
                    latest_map_id=int(latest_map["id"]) if latest_map else None,
                    latest_reading_id=int(latest_reading["id"]) if latest_reading else None,
                )
                if active is not None
                else None
            )
            history = [
                _publication_view(
                    conn,
                    row,
                    plate_id=plate_id,
                    latest_map_id=int(latest_map["id"]) if latest_map else None,
                    latest_reading_id=int(latest_reading["id"]) if latest_reading else None,
                )
                for row in pub_rows
            ]
            map_versions = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, seq, source, note, original_filename, sha256, byte_size, created_at "
                    "FROM plate_map_versions WHERE plate_id = ? ORDER BY seq DESC",
                    (plate_id,),
                ).fetchall()
            ]
            reading_versions = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, seq, source, note, original_filename, sha256, byte_size, created_at, "
                    "warnings_json FROM reading_versions WHERE plate_id = ? ORDER BY seq DESC",
                    (plate_id,),
                ).fetchall()
            ]
            for version in reading_versions:
                version["warnings"] = _loads(version.pop("warnings_json")) or []
            return {
                "plate": plate,
                "active_publication": active_view,
                "publications": history,
                "latest_map_version": (
                    {
                        "id": int(latest_map["id"]),
                        "seq": int(latest_map["seq"]),
                        "created_at": latest_map["created_at"],
                    }
                    if latest_map
                    else None
                ),
                "latest_reading_version": (
                    {
                        "id": int(latest_reading["id"]),
                        "seq": int(latest_reading["seq"]),
                        "created_at": latest_reading["created_at"],
                    }
                    if latest_reading
                    else None
                ),
                "map_versions": map_versions,
                "reading_versions": reading_versions,
                "quality_floor": config.quality_floor(),
            }
    finally:
        conn.close()


def get_publication(plate_id: int, publication_id: int) -> dict[str, Any]:
    conn = db.connect()
    try:
        with db.transaction(conn, immediate=False):
            _plate_row(conn, plate_id)
            row = conn.execute(
                "SELECT * FROM publications WHERE id = ? AND plate_id = ?",
                (publication_id, plate_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"发布版 {publication_id} 不存在")
            latest_map = _latest_version(conn, "plate_map_versions", plate_id, required=False)
            latest_reading = _latest_version(conn, "reading_versions", plate_id, required=False)
            return _publication_view(
                conn,
                row,
                plate_id=plate_id,
                latest_map_id=int(latest_map["id"]) if latest_map else None,
                latest_reading_id=int(latest_reading["id"]) if latest_reading else None,
            )
    finally:
        conn.close()


def get_version_file(plate_id: int, version_id: int, kind: str) -> tuple[Path, sqlite3.Row]:
    if kind not in {"map", "reading"}:
        raise NotFoundError("未知文件类型")
    table = "plate_map_versions" if kind == "map" else "reading_versions"
    conn = db.connect()
    try:
        with db.transaction(conn, immediate=False):
            _plate_row(conn, plate_id)
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ? AND plate_id = ?",
                (version_id, plate_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("输入版本不存在")
            path = resolve_stored_file(row["stored_name"], plate_id)
            return path, row
    finally:
        conn.close()
