"""SQLite connection handling and the database schema.

Design notes (see SUBMISSION.md for the full rationale):

* ``ingested_files`` records every file by content hash. Re-ingesting a file
  with the same sha256 is skipped, which makes ``ingest`` idempotent.
* ``lead_rows`` has one row per non-blank CSV row and never holds PII. All
  lead counts in data_quality.json and rejections.csv are derived from it.
* ``leads`` is the merged, current view of each lead. Identity is
  ``(workflow_id, phone_hash)``, enforced by a UNIQUE constraint.
* ``events`` is the append-only interaction ledger. Triggers block UPDATE and
  DELETE. Orphan status is *not* stored: events are linked to leads at report
  time, so an orphan from drop_1 is linked automatically once drop_2 brings
  its lead.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ingested_files (
    file_id     INTEGER PRIMARY KEY,  -- increasing, so it is also the ingest order
    sha256      TEXT NOT NULL UNIQUE,
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id TEXT,                 -- leads files only
    ingested_at TEXT NOT NULL,
    -- events files only: counters for lines that never reach the ledger
    lines_total INTEGER,
    malformed   INTEGER,
    duplicates  INTEGER
);

-- Small key/value store. Holds a check value for the PII keys, so a database
-- can't be silently written with different keys than it was created with.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lead_rows (
    file_id        INTEGER NOT NULL REFERENCES ingested_files (file_id),
    row_number     INTEGER NOT NULL,
    workflow_id    TEXT NOT NULL,
    batch_id       TEXT NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('created', 'merged', 'rejected')),
    reason         TEXT,
    field_warnings INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (file_id, row_number),
    CHECK ((outcome = 'rejected') = (reason IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id      INTEGER PRIMARY KEY,
    workflow_id  TEXT NOT NULL,
    phone_hash   TEXT NOT NULL,
    phone_last4  TEXT NOT NULL CHECK (length(phone_last4) = 4),
    pii_enc      TEXT NOT NULL,  -- encrypted JSON of every pii field (incl. phone)
    external_ref TEXT NOT NULL,
    batch_id     TEXT NOT NULL,  -- from the first valid row; never updated
    attributes   TEXT NOT NULL,  -- JSON of the remaining normalised fields
    UNIQUE (workflow_id, phone_hash)
);

CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES ingested_files (file_id),
    event_type  TEXT,
    workflow_id TEXT,
    call_id     TEXT,
    occurred_at TEXT,  -- UTC ISO-8601; NULL if unparseable
    phone_hash  TEXT,  -- NULL if the phone is missing or invalid
    payload     TEXT NOT NULL
);
-- No secondary indexes on events on purpose: reports read the whole ledger in
-- one scan, and each extra random-key index roughly doubled bulk-insert time.

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events ledger is append-only');
END;
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection with explicit transaction control.

    ``isolation_level=None`` stops the sqlite3 module from opening transactions
    implicitly; callers use :func:`transaction` instead. (The ``autocommit``
    attribute would do the same but needs Python 3.12, and we support 3.11.)
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # off by default in SQLite
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")  # safe with WAL, much faster bulk loads
    # 64 MB page cache (negative = KiB). The 2 MB default thrashes on the
    # random-key indexes during large ingests.
    conn.execute("PRAGMA cache_size = -65536")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block atomically: COMMIT on success, ROLLBACK on any exception.

    Each input file is ingested inside one transaction, so a crash never
    leaves a file half-ingested or marked as done.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't exist. Safe to call on every run."""
    with transaction(conn):
        for statement in _split_statements(SCHEMA_SQL):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _split_statements(script: str) -> list[str]:
    """Split the schema into statements, keeping trigger bodies intact.

    ``executescript`` would be simpler, but it commits any open transaction
    first, which would break :func:`transaction`.
    """
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    return statements
