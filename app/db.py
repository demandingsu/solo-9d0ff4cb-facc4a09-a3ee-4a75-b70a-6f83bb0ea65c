"""SQLite connection management and schema.

Every mutation runs inside a ``BEGIN IMMEDIATE`` transaction so that version
number allocation, "mark the active publication stale" and publication swaps
are serialised.  Read paths use explicit transactions to get a consistent
snapshot across several tables.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS plates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plate_map_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_id INTEGER NOT NULL REFERENCES plates(id),
    seq INTEGER NOT NULL,
    source TEXT NOT NULL,
    note TEXT,
    original_filename TEXT,
    stored_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    layout_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (plate_id, seq)
);

CREATE TABLE IF NOT EXISTS reading_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_id INTEGER NOT NULL REFERENCES plates(id),
    seq INTEGER NOT NULL,
    source TEXT NOT NULL,
    note TEXT,
    original_filename TEXT,
    stored_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    readings_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (plate_id, seq)
);

CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_id INTEGER NOT NULL REFERENCES plates(id),
    seq INTEGER NOT NULL,
    map_version_id INTEGER NOT NULL REFERENCES plate_map_versions(id),
    reading_version_id INTEGER NOT NULL REFERENCES reading_versions(id),
    status TEXT NOT NULL CHECK (
        status IN ('published', 'stale', 'superseded', 'failed')
    ),
    is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    trigger TEXT NOT NULL,
    result_json TEXT,
    qc_json TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    stale_at TEXT,
    stale_reason TEXT,
    superseded_by INTEGER,
    UNIQUE (plate_id, seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_publications_one_active
    ON publications(plate_id) WHERE is_active = 1;

CREATE INDEX IF NOT EXISTS ix_publications_plate_seq
    ON publications(plate_id, seq DESC);

-- ---------------------------------------------------------------------------
-- Loading-chain verification (pipetting provenance)

CREATE TABLE IF NOT EXISTS transfer_log_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_plate_id INTEGER NOT NULL REFERENCES plates(id),
    target_plate_id INTEGER NOT NULL REFERENCES plates(id),
    seq INTEGER NOT NULL,
    source TEXT NOT NULL,
    note TEXT,
    original_filename TEXT,
    stored_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    operations_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_plate_id, target_plate_id, seq)
);

CREATE TABLE IF NOT EXISTS chain_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_plate_id INTEGER NOT NULL REFERENCES plates(id),
    target_plate_id INTEGER NOT NULL REFERENCES plates(id),
    seq INTEGER NOT NULL,
    transfer_log_version_id INTEGER NOT NULL REFERENCES transfer_log_versions(id),
    source_map_version_id INTEGER NOT NULL REFERENCES plate_map_versions(id),
    target_map_version_id INTEGER NOT NULL REFERENCES plate_map_versions(id),
    status TEXT NOT NULL CHECK (
        status IN ('completed', 'stale', 'superseded', 'failed')
    ),
    is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    trigger TEXT NOT NULL,
    result_json TEXT,
    summary_json TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    stale_at TEXT,
    stale_reason TEXT,
    superseded_by INTEGER,
    UNIQUE (source_plate_id, target_plate_id, seq)
);

CREATE TABLE IF NOT EXISTS chain_verification_plates (
    verification_id INTEGER NOT NULL REFERENCES chain_verifications(id),
    plate_id INTEGER NOT NULL REFERENCES plates(id),
    map_version_id INTEGER NOT NULL REFERENCES plate_map_versions(id),
    role TEXT NOT NULL CHECK (role IN ('source', 'target', 'intermediate')),
    PRIMARY KEY (verification_id, plate_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_chain_verifications_one_active
    ON chain_verifications(source_plate_id, target_plate_id) WHERE is_active = 1;

CREATE INDEX IF NOT EXISTS ix_chain_verifications_target_seq
    ON chain_verifications(target_plate_id, seq DESC);

CREATE INDEX IF NOT EXISTS ix_chain_verification_plates_plate
    ON chain_verification_plates(plate_id);

CREATE TRIGGER IF NOT EXISTS trg_transfer_log_versions_immutable_update
BEFORE UPDATE ON transfer_log_versions
BEGIN SELECT RAISE(ABORT, 'transfer log versions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_transfer_log_versions_immutable_delete
BEFORE DELETE ON transfer_log_versions
BEGIN SELECT RAISE(ABORT, 'transfer log versions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_plate_map_versions_immutable_update
BEFORE UPDATE ON plate_map_versions
BEGIN SELECT RAISE(ABORT, 'input versions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_reading_versions_immutable_update
BEFORE UPDATE ON reading_versions
BEGIN SELECT RAISE(ABORT, 'input versions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_plate_map_versions_immutable_delete
BEFORE DELETE ON plate_map_versions
BEGIN SELECT RAISE(ABORT, 'input versions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_reading_versions_immutable_delete
BEFORE DELETE ON reading_versions
BEGIN SELECT RAISE(ABORT, 'input versions are immutable'); END;
"""


def connect() -> sqlite3.Connection:
    path = config.database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    config.uploads_dir().mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version = 2")
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        conn.execute("COMMIT")


def next_seq(conn: sqlite3.Connection, table: str, plate_id: int) -> int:
    # ``table`` is only ever supplied by internal callers, never user input.
    if table not in {"plate_map_versions", "reading_versions", "publications"}:
        raise ValueError("unsupported table")
    row = conn.execute(
        f"SELECT COALESCE(MAX(seq), 0) AS max_seq FROM {table} WHERE plate_id = ?",
        (plate_id,),
    ).fetchone()
    return int(row["max_seq"]) + 1


def next_pair_seq(
    conn: sqlite3.Connection, table: str, source_plate_id: int, target_plate_id: int
) -> int:
    """Allocate the next sequence for a source/target chain inside a transaction."""
    if table not in {"transfer_log_versions", "chain_verifications"}:
        raise ValueError("unsupported table")
    row = conn.execute(
        f"SELECT COALESCE(MAX(seq), 0) AS max_seq FROM {table} "
        "WHERE source_plate_id = ? AND target_plate_id = ?",
        (source_plate_id, target_plate_id),
    ).fetchone()
    return int(row["max_seq"]) + 1
