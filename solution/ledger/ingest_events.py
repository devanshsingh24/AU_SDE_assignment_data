"""Event ingestion into the append-only ledger. Spec: Assignment A › Rules › Event files.

For each ``<drop>/events/*.jsonl`` (filename order), every non-blank line is:

1. **Malformed** (bad UTF-8, not JSON, not an object, no event_id) → counted, skipped.
2. **Duplicate** (event_id already in the ledger, from any drop) → counted, skipped.
3. Otherwise appended to ``events``.

Orphan and unknown-disposition checks (spec steps 3 and 4) are *not* done here.
Whether an event is an orphan depends on leads that may arrive in a later drop,
so they are resolved at report time from the stored events. That keeps the
final counts correct and independent of ingest order.

The raw line is never stored, because it contains the plaintext phone: only
its keyed hash is kept.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ledger.db import already_ingested, transaction
from ledger.normalize import normalize_phone, parse_timestamp
from ledger.pii import Pii


@dataclass
class EventFileResult:
    path: str
    skipped: bool = False
    lines: int = 0
    stored: int = 0
    malformed: int = 0
    duplicates: int = 0


def parse_event_line(raw: bytes) -> dict | None:
    """Decode one JSONL line, or return None if it is malformed. Pure."""
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(event, dict):
        return None
    event_id = event.get("event_id")
    if event_id is None or str(event_id).strip() == "":
        return None
    event["event_id"] = str(event_id)
    return event


def ingest_events(
    conn: sqlite3.Connection,
    pii: Pii,
    drop_dir: Path,
    on_file: Callable[[EventFileResult], None] | None = None,
) -> list[EventFileResult]:
    """Ingest every events file of one drop, in filename order.

    ``on_file`` is called as soon as each file is done.
    """
    known = _known_event_ids(conn)  # loaded once per drop, not once per file
    results = []
    for path in sorted((drop_dir / "events").glob("*.jsonl")):
        results.append(ingest_event_file(conn, pii, path, known))
        if on_file:
            on_file(results[-1])
    return results


def ingest_event_file(
    conn: sqlite3.Connection, pii: Pii, path: Path, known: set[str] | None = None
) -> EventFileResult:
    """Ingest one file. ``known`` is the set of event_ids already in the ledger.

    Duplicates are detected against that set so the whole file can go in with
    one ``executemany``. The event_id PRIMARY KEY stays as a backstop: if the
    set were ever wrong, the insert would fail loudly rather than double-count.
    """
    if known is None:
        known = _known_event_ids(conn)
    result = EventFileResult(path=str(path))
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if already_ingested(conn, path, sha256, "events"):
        result.skipped = True
        return result

    new_ids: set[str] = set()
    with transaction(conn):
        file_id = conn.execute(
            "INSERT INTO ingested_files (sha256, path, kind, ingested_at) VALUES (?, ?, 'events', ?)",
            (sha256, str(path), datetime.now(UTC).isoformat()),
        ).lastrowid

        rows = []
        with path.open("rb") as f:
            for raw in f:
                if not raw.strip():
                    continue  # spec: events_total counts non-empty lines only
                result.lines += 1
                event = parse_event_line(raw)
                if event is None:
                    result.malformed += 1
                    continue
                event_id = event["event_id"]
                if event_id in known or event_id in new_ids:
                    result.duplicates += 1  # first copy wins, whichever drop it came in
                    continue
                new_ids.add(event_id)
                rows.append(
                    (
                        event_id,
                        file_id,
                        event.get("event_type"),
                        event.get("workflow_id"),
                        event.get("call_id"),
                        _utc_timestamp(event.get("occurred_at")),
                        _phone_hash(pii, event.get("phone")),
                        json.dumps(event.get("payload") or {}),
                    )
                )

        conn.executemany(
            "INSERT INTO events (event_id, file_id, event_type, workflow_id, call_id,"
            " occurred_at, phone_hash, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        result.stored = len(rows)
        conn.execute(
            "UPDATE ingested_files SET lines_total = ?, malformed = ?, duplicates = ? WHERE file_id = ?",
            (result.lines, result.malformed, result.duplicates, file_id),
        )
    # Only after COMMIT: a rolled-back file must not mark its ids as known.
    known |= new_ids
    return result


def _known_event_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT event_id FROM events")}


def _utc_timestamp(value) -> str | None:
    """UTC ISO-8601 with fixed width, so sorting the strings sorts by time."""
    if not isinstance(value, str):
        return None
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def _phone_hash(pii: Pii, value) -> str | None:
    """Keyed hash of the canonical phone; None if missing or invalid."""
    if value is None:
        return None
    canonical = normalize_phone(str(value))
    return pii.phone_hash(canonical) if canonical else None
