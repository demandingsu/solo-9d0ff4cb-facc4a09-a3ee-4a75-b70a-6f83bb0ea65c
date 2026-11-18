"""Service layer for loading-chain verification.

The lifecycle mirrors publications:

* Every transfer-log upload creates an immutable ``transfer_log_versions`` row.
  In the same ``BEGIN IMMEDIATE`` transaction the active verification for that
  source/target pair becomes ``stale`` (still viewable, pinned to its inputs).
* A plate-map correction marks every active verification that pinned the old
  map of that plate stale.  This includes intermediate plates.
* A verification run reads the newest transfer log and the newest maps of all
  plates referenced by the log inside one write transaction, then stores
  concrete version ids for each of them.  A concurrent recompute can therefore
  only publish a complete result built from one committed input batch.
* A structurally invalid run (unknown/blank/non-sample wells, a read from an
  empty well, duplicate operations, log not referencing the selected plates)
  is recorded as ``failed``.  It never becomes active and never replaces a
  result; the previous result, if stale, stays viewable.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from . import chain, config, db, parsing

STATUS_COMPLETED = "completed"
STATUS_STALE = "stale"
STATUS_SUPERSEDED = "superseded"
STATUS_FAILED = "failed"

TRIGGER_UPLOAD = "upload"
TRIGGER_RECOMPUTE = "recompute"

# Checks that decide whether a report may be published at all.  Findings such
# as a mismatch, a missing well or a mixed well are trustworthy *conclusions*
# about the data, so they stay visible in a completed report; a structurally
# broken log (invalid wells, unreadable plate, no sample wells) is not.
STRUCTURAL_CHECKS = {
    "operations_valid",
    "target_sample_wells",
    "log_references_selected_plates",
}


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None) -> Any:
    return json.loads(value) if value else None


def _now() -> str:
    from .service import utcnow

    return utcnow()


# ---------------------------------------------------------------------------
# Small data helpers (kept local so service.py need not expose them)


def _plate(conn: sqlite3.Connection, plate_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM plates WHERE id = ?", (plate_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"板 {plate_id} 不存在")
    return row


def _latest_map(
    conn: sqlite3.Connection, plate_id: int, *, required: bool = True
) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM plate_map_versions WHERE plate_id = ? ORDER BY seq DESC LIMIT 1",
        (plate_id,),
    ).fetchone()
    if row is None and required:
        name = conn.execute("SELECT name FROM plates WHERE id = ?", (plate_id,)).fetchone()
        label = name["name"] if name else f"板 {plate_id}"
        raise ConflictError(f"板“{label}”尚未上传孔位图，无法核对上样链")
    return row


def _latest_log(
    conn: sqlite3.Connection, source_plate_id: int, target_plate_id: int, *, required: bool
) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM transfer_log_versions WHERE source_plate_id = ? AND target_plate_id = ? "
        "ORDER BY seq DESC LIMIT 1",
        (source_plate_id, target_plate_id),
    ).fetchone()
    if row is None and required:
        raise ConflictError("这一对源板/待核板还没有上传转移日志")
    return row


def _safe_name(name: str | None) -> str:
    if not name:
        return "transfer.csv"
    base = Path(name.replace("\\", "/")).name.strip()
    return (base or "transfer.csv")[:255]


def _store_log_file(
    source_plate_id: int, target_plate_id: int, data: bytes, filename: str
) -> tuple[str, int, str]:
    digest = hashlib.sha256(data).hexdigest()
    suffix = Path(filename).suffix.lower()
    if not suffix or len(suffix) > 12 or not suffix[1:].isalnum():
        suffix = ".txt"
    directory = config.uploads_dir() / "chains" / f"{source_plate_id}-{target_plate_id}"
    directory.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    target = directory / stored_name
    temporary = directory / f".{stored_name}.tmp"
    temporary.write_bytes(data)
    os.replace(temporary, target)
    return stored_name, len(data), digest


def _remove_log_file(stored_name: str, source_plate_id: int, target_plate_id: int) -> None:
    try:
        (
            config.uploads_dir()
            / "chains"
            / f"{source_plate_id}-{target_plate_id}"
            / _stored_basename(stored_name)
        ).unlink()
    except FileNotFoundError:
        pass


def resolve_log_file(row: sqlite3.Row) -> Path:
    base = (
        config.uploads_dir()
        / "chains"
        / f"{int(row['source_plate_id'])}-{int(row['target_plate_id'])}"
    ).resolve()
    target = (base / str(row["stored_name"])).resolve()
    if target.parent != base or not target.is_file():
        raise NotFoundError("转移日志原文件不存在")
    return target


def _stored_basename(stored_name: str) -> str:
    return stored_name.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Invalidation hooks (called inside the caller's write transaction)


def _stale_resons(existing: str | None, reason: str) -> str:
    parts = [part for part in (existing or "").split("；") if part]
    for part in (item.strip() for item in reason.split("；")):
        if part and part not in parts:
            parts.append(part)
    return "；".join(parts)


def mark_transfer_log_changed(
    conn: sqlite3.Connection,
    source_plate_id: int,
    target_plate_id: int,
    seq: int,
    now: str,
) -> int | None:
    """Mark the active verification of this pair stale; it stays viewable."""
    row = conn.execute(
        "SELECT * FROM chain_verifications WHERE source_plate_id = ? AND target_plate_id = ? "
        "AND is_active = 1",
        (source_plate_id, target_plate_id),
    ).fetchone()
    if row is None:
        return None
    reason = f"转移日志已更新至 v{seq}"
    if row["status"] == STATUS_COMPLETED:
        conn.execute(
            "UPDATE chain_verifications SET status = ?, stale_at = ?, stale_reason = ? WHERE id = ?",
            (STATUS_STALE, now, reason, row["id"]),
        )
        return int(row["id"])
    if row["status"] == STATUS_STALE:
        conn.execute(
            "UPDATE chain_verifications SET stale_reason = ? WHERE id = ?",
            (_stale_resons(row["stale_reason"], reason), row["id"]),
        )
    return None


def mark_plate_map_changed(
    conn: sqlite3.Connection, plate_id: int, seq: int, now: str
) -> list[int]:
    """Invalidate every active verification whose pinned maps include *plate_id*."""
    rows = conn.execute(
        """
        SELECT DISTINCT v.id, v.status, v.stale_reason
        FROM chain_verifications v
        JOIN chain_verification_plates p ON p.verification_id = v.id
        WHERE p.plate_id = ? AND v.is_active = 1
          AND v.status IN ('completed', 'stale')
        """,
        (plate_id,),
    ).fetchall()
    reason = f"孔位图已更新至 v{seq}"
    marked: list[int] = []
    for row in rows:
        if row["status"] == STATUS_COMPLETED:
            conn.execute(
                "UPDATE chain_verifications SET status = ?, stale_at = ?, stale_reason = ? "
                "WHERE id = ?",
                (STATUS_STALE, now, reason, row["id"]),
            )
            marked.append(int(row["id"]))
        else:
            conn.execute(
                "UPDATE chain_verifications SET stale_reason = ? WHERE id = ?",
                (_stale_resons(row["stale_reason"], reason), row["id"]),
            )
    return marked


# ---------------------------------------------------------------------------
# Running a verification


def _context(
    plate_row: sqlite3.Row, map_row: sqlite3.Row, role: str
) -> dict[str, Any]:
    layout = _loads(map_row["layout_json"]) or {}
    wells = layout.get("wells", {})
    samples = {
        well: entry["sample_id"]
        for well, entry in wells.items()
        if entry.get("role") == "sample" and entry.get("sample_id")
    }
    roles = {well: entry.get("role") for well, entry in wells.items()}
    return {
        "plate_id": int(plate_row["id"]),
        "name": plate_row["name"],
        "role": role,
        "map_version_id": int(map_row["id"]),
        "map_seq": int(map_row["seq"]),
        "samples": samples,
        "roles": roles,
    }


def _build_contexts(
    conn: sqlite3.Connection, source_plate_id: int, target_plate_id: int, operations: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    source = _plate(conn, source_plate_id)
    target = _plate(conn, target_plate_id)
    source_map = _latest_map(conn, source_plate_id)
    target_map = _latest_map(conn, target_plate_id)
    assert source_map is not None and target_map is not None

    contexts: dict[str, dict[str, Any]] = {
        source["name"]: _context(source, source_map, "source"),
        target["name"]: _context(target, target_map, "target"),
    }
    unresolved: dict[str, str] = {}

    for operation in operations:
        for name in (operation["source_plate"], operation["target_plate"]):
            if name in contexts or name in unresolved or name in {source["name"], target["name"]}:
                continue
            row = conn.execute("SELECT * FROM plates WHERE name = ?", (name,)).fetchone()
            if row is None:
                unresolved[name] = "没有同名板"
                continue
            map_row = _latest_map(conn, int(row["id"]), required=False)
            if map_row is None:
                unresolved[name] = "尚未上传孔位图"
                continue
            contexts[name] = _context(row, map_row, "intermediate")
    return contexts, unresolved


def _prepare_result(
    conn: sqlite3.Connection,
    source_plate_id: int,
    target_plate_id: int,
    log_row: sqlite3.Row,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    source = _plate(conn, source_plate_id)
    target = _plate(conn, target_plate_id)
    operations = _loads(log_row["operations_json"]) or []
    contexts, unresolved = _build_contexts(conn, source_plate_id, target_plate_id, operations)
    simulation = chain.simulate(
        contexts,
        operations,
        source_plate=source["name"],
        target_plate=target["name"],
    )

    checks = simulation["checks"]
    issues = simulation["issues"]

    source_is_read = any(
        operation["source_plate"] == source["name"] for operation in operations
    )
    target_is_written = any(
        operation["target_plate"] == target["name"] for operation in operations
    )
    detail_parts: list[str] = []
    if not source_is_read:
        detail_parts.append(f"日志没有从所选源板“{source['name']}”取样的操作")
    if not target_is_written:
        detail_parts.append(f"日志没有向所选待核板“{target['name']}”加样的操作")
    if unresolved:
        detail_parts.append(
            "无法解析的板：" + "、".join(f"{name}（{why}）" for name, why in sorted(unresolved.items()))
        )
    check = {
        "id": "log_references_selected_plates",
        "ok": source_is_read and target_is_written and not unresolved,
        "label": "转移日志从所选源板取样、向所选待核板加样，且引用的板都可解析",
        "detail": "；".join(detail_parts),
    }
    checks.append(check)
    if not check["ok"]:
        issues.append(f"{check['label']}：{check['detail']}")

    structural_ok = all(
        item["ok"] for item in checks if item["id"] in STRUCTURAL_CHECKS
    )
    simulation["conclusions_trustworthy"] = structural_ok
    simulation["summary"]["trustworthy"] = structural_ok
    simulation["checks"] = checks
    simulation["issues"] = issues
    if unresolved:
        simulation["invalid_operations"].extend(
            {
                "operation": None,
                "source_plate": name,
                "source_well": "",
                "target_plate": name,
                "target_well": "",
                "reason": why,
            }
            for name, why in sorted(unresolved.items())
        )
    return simulation, contexts


def _insert_verification(
    conn: sqlite3.Connection,
    *,
    source_plate_id: int,
    target_plate_id: int,
    source_name: str,
    target_name: str,
    log_row: sqlite3.Row,
    contexts: dict[str, dict[str, Any]],
    result: dict[str, Any],
    trigger: str,
    stale_reasons: list[str],
) -> dict[str, Any]:
    now = _now()
    seq = db.next_pair_seq(conn, "chain_verifications", source_plate_id, target_plate_id)
    previous = conn.execute(
        "SELECT * FROM chain_verifications WHERE source_plate_id = ? AND target_plate_id = ? "
        "AND is_active = 1",
        (source_plate_id, target_plate_id),
    ).fetchone()

    source_ctx = contexts[source_name]
    target_ctx = contexts[target_name]
    if source_ctx["map_version_id"] <= 0 or target_ctx["map_version_id"] <= 0:
        raise ConflictError("所选板缺少孔位图")

    summary = {
        "checks": result["checks"],
        "issues": result["issues"],
        "summary": result["summary"],
        "invalid_operations": result["invalid_operations"],
        "conclusions_trustworthy": result["conclusions_trustworthy"],
        "unresolved_plates": {},
    }
    plate_rows = [
        (int(ctx["plate_id"]), int(ctx["map_version_id"]), role)
        for name, ctx in contexts.items()
        for role in [ctx["role"]]
    ]

    if not result["conclusions_trustworthy"]:
        error = "；".join(result["issues"]) or "核对无法完成"
        if previous is not None and previous["status"] == STATUS_STALE:
            conn.execute(
                "UPDATE chain_verifications SET stale_reason = ? WHERE id = ?",
                (_stale_resons(previous["stale_reason"], f"核对失败：{error}"), previous["id"]),
            )
        # A completed (still valid) previous result is left untouched; only an
        # explicit input change may make it stale.
        cursor = conn.execute(
            """
            INSERT INTO chain_verifications
                (source_plate_id, target_plate_id, seq, transfer_log_version_id,
                 source_map_version_id, target_map_version_id, status, is_active,
                 trigger, result_json, summary_json, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, ?)
            """,
            (
                source_plate_id,
                target_plate_id,
                seq,
                log_row["id"],
                source_ctx["map_version_id"],
                target_ctx["map_version_id"],
                STATUS_FAILED,
                trigger,
                _dumps(summary),
                error,
                now,
            ),
        )
        failed_id = int(cursor.lastrowid)
        for plate_id, map_version_id, role in plate_rows:
            conn.execute(
                "INSERT OR IGNORE INTO chain_verification_plates "
                "(verification_id, plate_id, map_version_id, role) VALUES (?, ?, ?, ?)",
                (failed_id, plate_id, map_version_id, role),
            )
        return {
            "outcome": "failed",
            "verification_id": failed_id,
            "verification_seq": seq,
            "active_id": int(previous["id"]) if previous else None,
            "active_is_stale": previous is not None and previous["status"] == STATUS_STALE,
            "summary": summary,
            "error": error,
        }

    if previous is not None:
        conn.execute(
            "UPDATE chain_verifications SET status = ?, is_active = 0 WHERE id = ?",
            (STATUS_SUPERSEDED, previous["id"]),
        )
    cursor = conn.execute(
        """
        INSERT INTO chain_verifications
            (source_plate_id, target_plate_id, seq, transfer_log_version_id,
             source_map_version_id, target_map_version_id, status, is_active,
             trigger, result_json, summary_json, error, created_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, ?, ?)
        """,
        (
            source_plate_id,
            target_plate_id,
            seq,
            log_row["id"],
            source_ctx["map_version_id"],
            target_ctx["map_version_id"],
            STATUS_COMPLETED,
            trigger,
            _dumps(
                {
                    "target_wells": result["target_wells"],
                    "chains": result["chains"],
                    "events": result["events"],
                    "mismatches": result["mismatches"],
                    "missing_wells": result["missing_wells"],
                    "mixed_wells": result["mixed_wells"],
                    "plate_roles": result["plate_roles"],
                }
            ),
            _dumps(summary),
            now,
            now,
        ),
    )
    new_id = int(cursor.lastrowid)
    for plate_id, map_version_id, role in plate_rows:
        conn.execute(
            "INSERT OR IGNORE INTO chain_verification_plates "
            "(verification_id, plate_id, map_version_id, role) VALUES (?, ?, ?, ?)",
            (new_id, plate_id, map_version_id, role),
        )
    if previous is not None:
        conn.execute(
            "UPDATE chain_verifications SET superseded_by = ? WHERE id = ?",
            (new_id, previous["id"]),
        )
    return {
        "outcome": "completed",
        "verification_id": new_id,
        "verification_seq": seq,
        "superseded_verification_id": int(previous["id"]) if previous else None,
        "summary": summary,
    }


def verify_chain(
    source_plate_id: int,
    target_plate_id: int,
    *,
    log_data: bytes | None = None,
    log_filename: str | None = None,
    note: str | None = None,
    trigger: str = TRIGGER_UPLOAD,
) -> dict[str, Any]:
    """Store a transfer-log version (optional) and run one verification."""
    if source_plate_id == target_plate_id:
        raise ConflictError("源板与待核板不能是同一块板")
    clean_note = (note or "").strip() or None
    if clean_note and len(clean_note) > 500:
        raise parsing.InputError("备注不能超过 500 个字符")

    parsed = chain.parse_transfer_log(log_data) if log_data is not None else None
    # The data above is fully parsed before the transaction starts; malformed
    # logs therefore create no version and cannot invalidate an old result.

    conn = db.connect()
    try:
        with db.transaction(conn, immediate=True):
            _plate(conn, source_plate_id)
            _plate(conn, target_plate_id)
            now = _now()
            new_seq: int | None = None
            stored_name: str | None = None
            if parsed is not None and log_data is not None:
                filename = _safe_name(log_filename)
                new_seq = db.next_pair_seq(
                    conn, "transfer_log_versions", source_plate_id, target_plate_id
                )
                stored_name, size, digest = _store_log_file(
                    source_plate_id, target_plate_id, log_data, filename
                )
                conn.execute(
                    """
                    INSERT INTO transfer_log_versions
                        (source_plate_id, target_plate_id, seq, source, note,
                         original_filename, stored_name, media_type, sha256, byte_size,
                         operations_json, warnings_json, created_at)
                    VALUES (?, ?, ?, 'upload', ?, ?, ?, 'text/csv', ?, ?, ?, ?, ?)
                    """,
                    (
                        source_plate_id,
                        target_plate_id,
                        new_seq,
                        clean_note,
                        filename,
                        stored_name,
                        digest,
                        size,
                        _dumps(parsed.operations),
                        _dumps(parsed.warnings),
                        now,
                    ),
                )
                marked = mark_transfer_log_changed(
                    conn, source_plate_id, target_plate_id, new_seq, now
                )
            else:
                marked = None

            log_row = _latest_log(conn, source_plate_id, target_plate_id, required=True)
            assert log_row is not None
            result, contexts = _prepare_result(
                conn, source_plate_id, target_plate_id, log_row
            )
            stale_reasons = [f"转移日志已更新至 v{new_seq}"] if new_seq else []
            source_name = _plate(conn, source_plate_id)["name"]
            target_name = _plate(conn, target_plate_id)["name"]
            outcome = _insert_verification(
                conn,
                source_plate_id=source_plate_id,
                target_plate_id=target_plate_id,
                source_name=source_name,
                target_name=target_name,
                log_row=log_row,
                contexts=contexts,
                result=result,
                trigger=trigger,
                stale_reasons=stale_reasons,
            )
            outcome.update(
                {
                    "stale_verification_id": marked,
                    "new_log_version": new_seq,
                    "log_version_seq": int(log_row["seq"]),
                }
            )
            return outcome
    except BaseException:
        if stored_name is not None:
            _remove_log_file(stored_name, source_plate_id, target_plate_id)
        raise
    finally:
        conn.close()


def _latest_map_ids(
    conn: sqlite3.Connection, plate_ids: list[int]
) -> dict[int, int]:
    result: dict[int, int] = {}
    for plate_id in plate_ids:
        row = _latest_map(conn, plate_id, required=False)
        if row is not None:
            result[plate_id] = int(row["id"])
    return result


def _verification_pins_latest_inputs(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    latest_log = _latest_log(
        conn, int(row["source_plate_id"]), int(row["target_plate_id"]), required=False
    )
    if latest_log is None or int(latest_log["id"]) != int(row["transfer_log_version_id"]):
        return False
    pins = conn.execute(
        "SELECT plate_id, map_version_id FROM chain_verification_plates WHERE verification_id = ?",
        (row["id"],),
    ).fetchall()
    if not pins:
        return False
    latest_maps = _latest_map_ids(conn, [int(pin["plate_id"]) for pin in pins])
    return all(latest_maps.get(int(pin["plate_id"])) == int(pin["map_version_id"]) for pin in pins)


def recompute_chain(target_plate_id: int, verification_id: int) -> dict[str, Any]:
    """Re-run using the newest committed log and plate maps for this pair."""
    conn = db.connect()
    try:
        with db.transaction(conn, immediate=True):
            row = conn.execute(
                "SELECT * FROM chain_verifications WHERE id = ? AND target_plate_id = ?",
                (verification_id, target_plate_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"核对记录 {verification_id} 不存在")
            source_plate_id = int(row["source_plate_id"])
            source_name = _plate(conn, source_plate_id)["name"]
            target_name = _plate(conn, target_plate_id)["name"]

            log_row = _latest_log(conn, source_plate_id, target_plate_id, required=True)
            assert log_row is not None
            active = conn.execute(
                "SELECT * FROM chain_verifications WHERE source_plate_id = ? AND target_plate_id = ? "
                "AND is_active = 1",
                (source_plate_id, target_plate_id),
            ).fetchone()

            if (
                active is not None
                and active["status"] == STATUS_COMPLETED
                and _verification_pins_latest_inputs(conn, active)
            ):
                return {
                    "outcome": "unchanged",
                    "verification_id": int(active["id"]),
                    "verification_seq": int(active["seq"]),
                    "message": "输入未变化，当前核对结果仍然有效。",
                }

            result, contexts = _prepare_result(
                conn, source_plate_id, target_plate_id, log_row
            )
            return _insert_verification(
                conn,
                source_plate_id=source_plate_id,
                target_plate_id=target_plate_id,
                source_name=source_name,
                target_name=target_name,
                log_row=log_row,
                contexts=contexts,
                result=result,
                trigger=TRIGGER_RECOMPUTE,
                stale_reasons=[],
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read models


def _version_file_summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "seq": int(row["seq"]),
        "original_filename": row["original_filename"],
        "sha256": row["sha256"],
        "byte_size": int(row["byte_size"]),
        "created_at": row["created_at"],
        "note": row["note"],
        "warnings": _loads(row["warnings_json"]) or [],
    }


def _plate_link(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    plate = conn.execute("SELECT name FROM plates WHERE id = ?", (row["plate_id"],)).fetchone()
    map_row = conn.execute(
        "SELECT seq, original_filename FROM plate_map_versions WHERE id = ?",
        (row["map_version_id"],),
    ).fetchone()
    return {
        "plate_id": int(row["plate_id"]),
        "name": plate["name"] if plate else "?",
        "role": row["role"],
        "map_version_id": int(row["map_version_id"]),
        "map_version_seq": int(map_row["seq"]) if map_row else None,
        "map_filename": map_row["original_filename"] if map_row else None,
    }


def _view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    log_row = conn.execute(
        "SELECT * FROM transfer_log_versions WHERE id = ?", (row["transfer_log_version_id"],)
    ).fetchone()
    source = conn.execute("SELECT * FROM plates WHERE id = ?", (row["source_plate_id"],)).fetchone()
    target = conn.execute("SELECT * FROM plates WHERE id = ?", (row["target_plate_id"],)).fetchone()
    plates = [
        _plate_link(conn, link)
        for link in conn.execute(
            "SELECT * FROM chain_verification_plates WHERE verification_id = ? "
            "ORDER BY CASE role WHEN 'source' THEN 0 WHEN 'intermediate' THEN 1 ELSE 2 END, plate_id",
            (row["id"],),
        ).fetchall()
    ]
    summary = _loads(row["summary_json"]) or {}
    superseded_by = None
    if row["superseded_by"]:
        superseded = conn.execute(
            "SELECT id, seq FROM chain_verifications WHERE id = ?", (row["superseded_by"],)
        ).fetchone()
        if superseded:
            superseded_by = {"id": int(superseded["id"]), "seq": int(superseded["seq"])}
    latest_log = _latest_log(
        conn, int(row["source_plate_id"]), int(row["target_plate_id"]), required=False
    )
    latest_maps = _latest_map_ids(conn, [link["plate_id"] for link in plates])
    map_drift = [
        link
        for link in plates
        if latest_maps.get(link["plate_id"]) != link["map_version_id"]
    ]
    log_drift = (
        latest_log is None
        or log_row is None
        or int(latest_log["id"]) != int(log_row["id"])
    )
    active_row = conn.execute(
        "SELECT id, seq, status FROM chain_verifications "
        "WHERE source_plate_id = ? AND target_plate_id = ? AND is_active = 1",
        (row["source_plate_id"], row["target_plate_id"]),
    ).fetchone()
    active_verification = None
    if active_row is not None and int(active_row["id"]) != int(row["id"]):
        active_verification = {
            "id": int(active_row["id"]),
            "seq": int(active_row["seq"]),
            "status": active_row["status"],
        }
    return {
        "id": int(row["id"]),
        "seq": int(row["seq"]),
        "status": row["status"],
        "active": bool(row["is_active"]),
        "active_verification": active_verification,
        "trigger": row["trigger"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "stale_at": row["stale_at"],
        "stale_reason": row["stale_reason"],
        "error": row["error"],
        "superseded_by": superseded_by,
        "source_plate": {"id": int(source["id"]), "name": source["name"]} if source else None,
        "target_plate": {"id": int(target["id"]), "name": target["name"]} if target else None,
        "log": _version_file_summary(log_row),
        "plates": plates,
        "inputs_are_latest": not map_drift and not log_drift,
        "outdated_plates": [
            {"name": link["name"], "pinned_seq": link["map_version_seq"]} for link in map_drift
        ],
        "log_outdated": log_drift,
        "checks": summary.get("checks", []),
        "issues": summary.get("issues", []),
        "summary": summary.get("summary", {}),
        "invalid_operations": summary.get("invalid_operations", []),
        "conclusions_trustworthy": summary.get("conclusions_trustworthy", False),
        "result": _loads(row["result_json"]),
    }


def get_chain_verification(verification_id: int, *, target_plate_id: int | None = None) -> dict[str, Any]:
    conn = db.connect()
    try:
        with db.transaction(conn, immediate=False):
            row = conn.execute(
                "SELECT * FROM chain_verifications WHERE id = ?", (verification_id,)
            ).fetchone()
            if row is None or (
                target_plate_id is not None and int(row["target_plate_id"]) != target_plate_id
            ):
                raise NotFoundError(f"核对记录 {verification_id} 不存在")
            return _view(conn, row)
    finally:
        conn.close()


def get_log_file(verification_id: int) -> tuple[Path, sqlite3.Row]:
    conn = db.connect()
    try:
        with db.transaction(conn, immediate=False):
            row = conn.execute(
                "SELECT v.*, l.stored_name, l.original_filename, l.sha256, l.byte_size "
                "FROM chain_verifications v JOIN transfer_log_versions l "
                "ON l.id = v.transfer_log_version_id WHERE v.id = ?",
                (verification_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("核对记录不存在")
            path = resolve_log_file(row)
            return path, row
    finally:
        conn.close()


def list_chain_verifications(
    *, target_plate_id: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """One row per source/target pair: the active result plus latest attempt."""
    conn = db.connect()
    try:
        with db.transaction(conn, immediate=False):
            if target_plate_id is None:
                rows = conn.execute(
                    "SELECT * FROM chain_verifications ORDER BY target_plate_id, source_plate_id, seq DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM chain_verifications WHERE target_plate_id = ? "
                    "ORDER BY source_plate_id, seq DESC",
                    (target_plate_id,),
                ).fetchall()
            grouped: dict[tuple[int, int], dict[str, sqlite3.Row]] = {}
            for row in rows:
                key = (int(row["source_plate_id"]), int(row["target_plate_id"]))
                group = grouped.setdefault(key, {})
                group.setdefault("latest", row)
                if row["is_active"]:
                    group["active"] = row
            result = []
            for group in grouped.values():
                chosen = group.get("active") or group["latest"]
                item = _view(conn, chosen)
                latest = group["latest"]
                item["latest_attempt"] = {
                    "id": int(latest["id"]),
                    "seq": int(latest["seq"]),
                    "status": latest["status"],
                    "created_at": latest["created_at"],
                    "error": latest["error"],
                    "is_active": bool(latest["is_active"]),
                }
                item["active"] = chosen is group.get("active")
                result.append(item)
            result.sort(
                key=lambda item: (
                    item["target_plate"]["name"] if item["target_plate"] else "",
                    item["source_plate"]["name"] if item["source_plate"] else "",
                )
            )
            return result[:limit]
    finally:
        conn.close()


def plate_chain_summary(plate_id: int) -> dict[str, Any]:
    """Verifications in which a plate participates, for the plate detail page.

    Shows the active result for each pair; when a pair has no active result the
    latest failed attempt is shown instead so a failed run is still visible.
    """
    conn = db.connect()
    try:
        with db.transaction(conn, immediate=False):
            rows = conn.execute(
                """
                SELECT v.*, p.role AS plate_role, p.map_version_id AS pinned_map_id
                FROM chain_verifications v
                JOIN chain_verification_plates p ON p.verification_id = v.id
                WHERE p.plate_id = ?
                ORDER BY v.target_plate_id, v.source_plate_id, v.seq DESC
                """,
                (plate_id,),
            ).fetchall()
            grouped: dict[tuple[int, int], dict[str, sqlite3.Row]] = {}
            for row in rows:
                key = (int(row["source_plate_id"]), int(row["target_plate_id"]))
                group = grouped.setdefault(key, {})
                group.setdefault("latest", row)
                if row["is_active"]:
                    group["active"] = row
            items = []
            for group in grouped.values():
                chosen = group.get("active") or group["latest"]
                view = _view(conn, chosen)
                view["active"] = chosen is group.get("active")
                view["this_plate_role"] = chosen["plate_role"]
                view["pinned_map_seq"] = conn.execute(
                    "SELECT seq FROM plate_map_versions WHERE id = ?",
                    (chosen["pinned_map_id"],),
                ).fetchone()["seq"]
                items.append(view)
            return {"verifications": items}
    finally:
        conn.close()


def latest_transfer_log(source_plate_id: int, target_plate_id: int) -> dict[str, Any] | None:
    """Metadata for the newest transfer log of a pair (used by the UI form)."""
    conn = db.connect()
    try:
        with db.transaction(conn, immediate=False):
            row = _latest_log(conn, source_plate_id, target_plate_id, required=False)
            return _version_file_summary(row) if row else None
    finally:
        conn.close()
