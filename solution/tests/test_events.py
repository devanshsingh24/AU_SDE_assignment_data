"""Tests for ledger.ingest_events."""

import json
import re
import sqlite3

import pytest

from ledger.db import IngestError
from ledger.ingest_events import ingest_event_file, ingest_events, parse_event_line


def event(event_id="evt_1", **overrides):
    base = {
        "event_id": event_id,
        "event_type": "call.initiated",
        "workflow_id": "motor_renewal",
        "call_id": "call_1",
        "occurred_at": "2026-10-05T13:20:21+05:30",
        "phone": "91-98765-43210",
        "payload": {},
    }
    return json.dumps({**base, **overrides})


def write_jsonl(path, *lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def stored(conn):
    return {r["event_id"]: dict(r) for r in conn.execute("SELECT * FROM events")}


def file_counters(conn):
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT lines_total, malformed, duplicates FROM ingested_files ORDER BY file_id"
        )
    ]


# ---------- parse_event_line ----------


def test_parse_valid_line():
    assert parse_event_line(event().encode())["event_id"] == "evt_1"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(event().encode()[:25], id="truncated"),
        pytest.param(b"not json", id="not-json"),
        pytest.param(b'["evt_1"]', id="json-array"),
        pytest.param(b'{"event_type": "call.initiated"}', id="no-event-id"),
        pytest.param(b'{"event_id": ""}', id="empty-event-id"),
        pytest.param(b'{"event_id": null}', id="null-event-id"),
        pytest.param(b'{"event_id": "\xff"}', id="invalid-utf8"),
    ],
)
def test_parse_malformed_line(raw):
    assert parse_event_line(raw) is None


def test_numeric_event_id_is_kept_as_string():
    assert parse_event_line(b'{"event_id": 42}')["event_id"] == "42"


# ---------- counting ----------


def test_counts_malformed_duplicates_and_ignores_blank_lines(conn, pii, tmp_path):
    path = write_jsonl(
        tmp_path / "events_0001.jsonl",
        event("evt_1"),
        "",
        "   ",
        event("evt_2")[:30],
        event("evt_1"),
        event("evt_3"),
    )
    result = ingest_event_file(conn, pii, path)
    assert (result.lines, result.stored, result.malformed, result.duplicates) == (4, 2, 1, 1)
    assert result.lines == result.stored + result.malformed + result.duplicates
    assert file_counters(conn) == [(4, 1, 1)]


def test_first_copy_of_a_duplicate_wins(conn, pii, tmp_path):
    path = write_jsonl(
        tmp_path / "events_0001.jsonl",
        event("evt_1", call_id="first"),
        event("evt_1", call_id="second"),
    )
    ingest_event_file(conn, pii, path)
    assert stored(conn)["evt_1"]["call_id"] == "first"


def test_duplicate_across_drops(conn, pii, tmp_path):
    write_jsonl(tmp_path / "drop_1" / "events" / "events_0001.jsonl", event("evt_1"))
    write_jsonl(tmp_path / "drop_2" / "events" / "events_1001.jsonl", event("evt_1"), event("evt_2"))
    ingest_events(conn, pii, tmp_path / "drop_1")
    (result,) = ingest_events(conn, pii, tmp_path / "drop_2")
    assert (result.stored, result.duplicates) == (1, 1)


def test_reingesting_a_file_changes_nothing(conn, pii, tmp_path):
    path = write_jsonl(tmp_path / "events_0001.jsonl", event("evt_1"), event("evt_1"), "oops")
    ingest_event_file(conn, pii, path)
    before = (stored(conn), file_counters(conn))

    assert ingest_event_file(conn, pii, path).skipped
    assert (stored(conn), file_counters(conn)) == before


# ---------- what gets stored ----------


@pytest.mark.parametrize(
    "occurred_at",
    [
        pytest.param("2026-10-05T07:50:21Z", id="utc"),
        pytest.param("2026-10-05T13:20:21+05:30", id="offset"),
        pytest.param("2026-10-05 13:20:21", id="naive-ist"),
    ],
)
def test_timestamps_are_stored_in_utc(conn, pii, tmp_path, occurred_at):
    ingest_event_file(conn, pii, write_jsonl(tmp_path / "e.jsonl", event(occurred_at=occurred_at)))
    assert stored(conn)["evt_1"]["occurred_at"] == "2026-10-05T07:50:21.000000+00:00"


def test_unparseable_timestamp_is_null_but_event_is_kept(conn, pii, tmp_path):
    ingest_event_file(conn, pii, write_jsonl(tmp_path / "e.jsonl", event(occurred_at="yesterday")))
    assert stored(conn)["evt_1"]["occurred_at"] is None


def test_phone_formats_hash_to_the_same_value(conn, pii, tmp_path):
    path = write_jsonl(
        tmp_path / "e.jsonl",
        event("evt_1", phone="9876543210"),
        event("evt_2", phone="(+91) 9876543210"),
        event("evt_3", phone="09876543210"),
    )
    ingest_event_file(conn, pii, path)
    hashes = {e["phone_hash"] for e in stored(conn).values()}
    assert hashes == {pii.phone_hash("+919876543210")}


def test_invalid_phone_is_stored_with_null_hash(conn, pii, tmp_path):
    ingest_event_file(conn, pii, write_jsonl(tmp_path / "e.jsonl", event(phone="022-2345-6789")))
    assert stored(conn)["evt_1"]["phone_hash"] is None


def test_override_without_phone_keeps_call_id(conn, pii, tmp_path):
    override = json.dumps(
        {
            "event_id": "evt_9",
            "event_type": "disposition.override",
            "workflow_id": "motor_renewal",
            "call_id": "call_7",
            "occurred_at": "2026-10-06T10:00:00Z",
            "payload": {
                "disposition_code": "renewed",
                "overridden_by": "supervisor_1",
                "reason": "qa_review",
            },
        }
    )
    ingest_event_file(conn, pii, write_jsonl(tmp_path / "e.jsonl", override))
    row = stored(conn)["evt_9"]
    assert row["phone_hash"] is None
    assert row["call_id"] == "call_7"
    assert json.loads(row["payload"])["disposition_code"] == "renewed"


def test_events_without_a_lead_are_stored_not_dropped(conn, pii, tmp_path):
    # No leads exist at all: these become orphans only at report time.
    ingest_event_file(conn, pii, write_jsonl(tmp_path / "e.jsonl", event("evt_1"), event("evt_2")))
    assert len(stored(conn)) == 2


def test_no_plaintext_phone_in_ledger(conn, pii, tmp_path):
    ingest_event_file(conn, pii, write_jsonl(tmp_path / "e.jsonl", event(phone="+91 98765 43210")))
    dump = " ".join(str(v) for r in conn.execute("SELECT * FROM events") for v in tuple(r))
    assert not re.search(r"98765", dump)


def test_ingested_events_cannot_be_modified(conn, pii, tmp_path):
    ingest_event_file(conn, pii, write_jsonl(tmp_path / "e.jsonl", event()))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET call_id = 'x'")


def test_duplicate_across_files_of_the_same_drop(conn, pii, tmp_path):
    write_jsonl(tmp_path / "drop" / "events" / "events_0001.jsonl", event("evt_1"))
    write_jsonl(tmp_path / "drop" / "events" / "events_0002.jsonl", event("evt_1"), event("evt_2"))
    first, second = ingest_events(conn, pii, tmp_path / "drop")
    assert (first.stored, second.stored, second.duplicates) == (1, 1, 1)


def test_primary_key_backstops_a_stale_known_set(conn, pii, tmp_path):
    # If the in-memory set of known ids were ever wrong, the database must
    # refuse the duplicate and roll the whole file back, not double-count.
    ingest_event_file(conn, pii, write_jsonl(tmp_path / "a.jsonl", event("evt_1")))
    with pytest.raises(sqlite3.IntegrityError):
        stale = write_jsonl(tmp_path / "b.jsonl", event("evt_2"), event("evt_1"))  # different bytes
        ingest_event_file(conn, pii, stale, known=set())
    assert set(stored(conn)) == {"evt_1"}  # evt_2 rolled back with the file
    assert conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 1


def test_changed_events_file_with_the_same_name_is_refused(conn, pii, tmp_path):
    path = write_jsonl(tmp_path / "drop_1" / "events" / "events_0001.jsonl", event("evt_1"))
    ingest_event_file(conn, pii, path)
    write_jsonl(path, event("evt_1"), event("evt_2"))
    with pytest.raises(IngestError, match="different content"):
        ingest_event_file(conn, pii, path)
    assert set(stored(conn)) == {"evt_1"}
