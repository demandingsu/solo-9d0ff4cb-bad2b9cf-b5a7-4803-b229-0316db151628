"""SQLite database setup and schema management.

Schema highlights (source-ledger model):
  * ``reading_sources`` keeps one row per uploaded CSV data row, *including*
    same-value retransmissions. A source is either active or withdrawn.
  * ``readings`` rows are never deleted: a reading is *effective* while it has
    at least one active source (``readings.active=1``). Losing the last active
    source flips it to inactive; a later re-upload re-activates it.
  * ``upload_batches`` gains the ``withdrawn`` status and a ``withdrawable``
    flag. Batches carried over from a legacy database (no per-row sources) are
    marked ``withdrawable=0`` and keep supplying data as before.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .config import DB_PATH, DATA_DIR


def get_db_path() -> Path:
    return Path(os.environ.get("WATER_DB_PATH", DB_PATH))


def get_connection(db_path: str | os.PathLike | None = None):
    path = Path(db_path) if db_path is not None else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


UPLOAD_BATCHES_DDL = """
CREATE TABLE IF NOT EXISTS upload_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_type      TEXT NOT NULL CHECK (batch_type IN ('reading','replacement')),
    filename        TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('committed','rejected','withdrawn')),
    row_count       INTEGER NOT NULL DEFAULT 0,
    reject_reason   TEXT,
    created_at      TEXT NOT NULL,
    withdrawable    INTEGER NOT NULL DEFAULT 1,
    withdrawn_at    TEXT
);
"""

READINGS_DDL = """
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES upload_batches(id),
    account     TEXT NOT NULL,
    meter       TEXT NOT NULL,
    read_at     TEXT NOT NULL,            -- ISO-8601 UTC, e.g. 2026-01-02T08:30:00Z
    reading_cum REAL NOT NULL CHECK (reading_cum >= 0),
    created_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    UNIQUE (account, meter, read_at)
);
"""

READING_SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS reading_sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     INTEGER NOT NULL REFERENCES upload_batches(id),
    reading_id   INTEGER NOT NULL REFERENCES readings(id),
    account      TEXT NOT NULL,
    meter        TEXT NOT NULL,
    read_at      TEXT NOT NULL,
    reading_cum  REAL NOT NULL,
    row_no       INTEGER NOT NULL,       -- 1-based data-row number inside the CSV
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);
"""

TABLE_DDL = (
    UPLOAD_BATCHES_DDL
    + READINGS_DDL
    + READING_SOURCES_DDL
    + """
CREATE TABLE IF NOT EXISTS replacements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES upload_batches(id),
    account         TEXT NOT NULL,
    old_meter       TEXT NOT NULL,
    new_meter       TEXT NOT NULL,
    switched_at     TEXT NOT NULL,       -- ISO-8601 UTC
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    status              TEXT NOT NULL CHECK (status IN ('active','expired','failed')),
    status_detail       TEXT,
    reading_batch_ids   TEXT NOT NULL,    -- comma separated committed reading batch ids at generation time
    replacement_batch_id INTEGER,          -- latest committed replacement batch id at generation time
    created_at          TEXT NOT NULL,
    superseded_at       TEXT
);

CREATE TABLE IF NOT EXISTS result_rows (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id       INTEGER NOT NULL REFERENCES generations(id),
    account             TEXT NOT NULL,
    meter               TEXT NOT NULL,
    interval_start      TEXT,
    interval_end        TEXT,
    start_reading       REAL,
    end_reading         REAL,
    consumption_m3      REAL,
    attributed_day      TEXT,             -- UTC YYYY-MM-DD of the later reading
    status              TEXT NOT NULL,    -- OK or REVIEW
    review_reason       TEXT,
    source_reading_ids  TEXT NOT NULL     -- comma separated readings.id
);
"""
)

INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_readings_lookup ON readings(account, meter, read_at);
CREATE INDEX IF NOT EXISTS idx_readings_active ON readings(active);
CREATE INDEX IF NOT EXISTS idx_rs_reading ON reading_sources(reading_id);
CREATE INDEX IF NOT EXISTS idx_rs_batch ON reading_sources(batch_id);
CREATE INDEX IF NOT EXISTS idx_replacements_acct ON replacements(account);
CREATE INDEX IF NOT EXISTS idx_results_gen ON result_rows(generation_id);
"""

# Backwards-compatible alias.
SCHEMA = TABLE_DDL + INDEX_DDL


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_legacy(conn: sqlite3.Connection) -> bool:
    """Upgrade a pre-ledger database in place.

    Legacy ``upload_batches`` rows have no per-row sources: they are flagged
    ``withdrawable=0`` so the UI/API refuse withdrawal while their readings
    keep feeding generation normally.

    SQLite stores the *parent table name* of each foreign key inside the child
    table's schema, so rebuilding ``upload_batches`` alone leaves stale
    references (``no such table: upload_batches_legacy``). Every table that
    references a rebuilt parent is therefore rebuilt too, following the
    documented "rebuild tables" procedure with foreign keys off in one
    transaction and an integrity check afterwards.

    Returns True when a table rebuild happened (the connection must then be
    reopened to clear SQLite's cached foreign-key schema).
    """
    rebuilt = False
    batch_cols = _table_columns(conn, "upload_batches")
    if batch_cols and "withdrawable" not in batch_cols:
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN")
            # --- rebuild upload_batches (new CHECK + withdrawable columns) ---
            conn.execute("ALTER TABLE upload_batches RENAME TO upload_batches_legacy")
            conn.execute(UPLOAD_BATCHES_DDL)
            conn.execute(
                """
                INSERT INTO upload_batches
                    (id, batch_type, filename, status, row_count,
                     reject_reason, created_at, withdrawable, withdrawn_at)
                SELECT id, batch_type, filename, status, row_count,
                       reject_reason, created_at, 0, NULL
                  FROM upload_batches_legacy
                """
            )
            conn.execute("DROP TABLE upload_batches_legacy")

            # --- rebuild readings (adds active; FK parent was just rebuilt) ---
            conn.execute("ALTER TABLE readings RENAME TO readings_legacy")
            conn.execute(READINGS_DDL)
            conn.execute(
                """
                INSERT INTO readings
                    (id, batch_id, account, meter, read_at, reading_cum,
                     created_at, active)
                SELECT id, batch_id, account, meter, read_at, reading_cum,
                       created_at, 1 FROM readings_legacy
                """
            )
            conn.execute("DROP TABLE readings_legacy")

            # --- recreate reading_sources (created before migration, its FK
            #     schema still points at the renamed readings_legacy table) ---
            conn.execute(
                """
                CREATE TABLE reading_sources_new (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id     INTEGER NOT NULL REFERENCES upload_batches(id),
                    reading_id   INTEGER NOT NULL REFERENCES readings(id),
                    account      TEXT NOT NULL,
                    meter        TEXT NOT NULL,
                    read_at      TEXT NOT NULL,
                    reading_cum  REAL NOT NULL,
                    row_no       INTEGER NOT NULL,
                    active       INTEGER NOT NULL DEFAULT 1,
                    created_at   TEXT NOT NULL
                )"""
            )
            if _table_columns(conn, "reading_sources"):
                conn.execute(
                    """INSERT INTO reading_sources_new
                       SELECT id, batch_id, reading_id, account, meter, read_at,
                              reading_cum, row_no, active, created_at
                         FROM reading_sources"""
                )
                conn.execute("DROP TABLE reading_sources")
            conn.execute("ALTER TABLE reading_sources_new RENAME TO reading_sources")

            # --- recreate child tables referencing the rebuilt parents ---
            conn.execute(
                """
                CREATE TABLE replacements_new (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id        INTEGER NOT NULL REFERENCES upload_batches(id),
                    account         TEXT NOT NULL,
                    old_meter       TEXT NOT NULL,
                    new_meter       TEXT NOT NULL,
                    switched_at     TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                )"""
            )
            if _table_columns(conn, "replacements"):
                conn.execute(
                    """INSERT INTO replacements_new
                           (id, batch_id, account, old_meter, new_meter,
                            switched_at, created_at)
                       SELECT id, batch_id, account, old_meter, new_meter,
                              switched_at, created_at FROM replacements"""
                )
                conn.execute("DROP TABLE replacements")
            conn.execute("ALTER TABLE replacements_new RENAME TO replacements")

            conn.execute(
                """
                CREATE TABLE generations_new (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    status              TEXT NOT NULL
                        CHECK (status IN ('active','expired','failed')),
                    status_detail       TEXT,
                    reading_batch_ids   TEXT NOT NULL,
                    replacement_batch_id INTEGER,
                    created_at          TEXT NOT NULL,
                    superseded_at       TEXT
                )"""
            )
            if _table_columns(conn, "generations"):
                conn.execute(
                    """INSERT INTO generations_new
                           (id, status, status_detail, reading_batch_ids,
                            replacement_batch_id, created_at, superseded_at)
                       SELECT id, status, status_detail, reading_batch_ids,
                              replacement_batch_id, created_at, superseded_at
                         FROM generations"""
                )
                conn.execute("DROP TABLE generations")
            conn.execute("ALTER TABLE generations_new RENAME TO generations")

            conn.execute(
                """
                CREATE TABLE result_rows_new (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    generation_id       INTEGER NOT NULL REFERENCES generations(id),
                    account             TEXT NOT NULL,
                    meter               TEXT NOT NULL,
                    interval_start      TEXT,
                    interval_end        TEXT,
                    start_reading       REAL,
                    end_reading         REAL,
                    consumption_m3      REAL,
                    attributed_day      TEXT,
                    status              TEXT NOT NULL,
                    review_reason       TEXT,
                    source_reading_ids  TEXT NOT NULL
                )"""
            )
            if _table_columns(conn, "result_rows"):
                conn.execute(
                    """INSERT INTO result_rows_new
                       SELECT id, generation_id, account, meter, interval_start,
                              interval_end, start_reading, end_reading,
                              consumption_m3, attributed_day, status,
                              review_reason, source_reading_ids
                         FROM result_rows"""
                )
                conn.execute("DROP TABLE result_rows")
            conn.execute("ALTER TABLE result_rows_new RENAME TO result_rows")
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        rebuilt = True
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:  # pragma: no cover - defensive integrity guard
            raise sqlite3.IntegrityError(
                f"legacy migration left foreign-key violations: {violations}"
            )

    reading_cols = _table_columns(conn, "readings")
    if reading_cols and "active" not in reading_cols:
        # Every legacy reading is an effective reading until proven otherwise.
        conn.execute(
            "ALTER TABLE readings ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
        )
        conn.commit()
    return rebuilt


def init_db(db_path: str | os.PathLike | None = None) -> Path:
    path = Path(db_path) if db_path is not None else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    try:
        # Create tables first, upgrade any legacy schema, and only then build
        # indexes that depend on the new columns (e.g. readings.active).
        conn.executescript(TABLE_DDL)
        conn.commit()
        needs_new_connection = _migrate_legacy(conn)
        if needs_new_connection:
            # SQLite caches the FK schema while foreign keys are disabled;
            # reopen the connection so later DML resolves the rebuilt table.
            conn.close()
            conn = get_connection(path)
        conn.executescript(INDEX_DDL)
        conn.commit()
    finally:
        conn.close()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return path
