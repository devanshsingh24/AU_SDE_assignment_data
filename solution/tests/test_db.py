"""Tests for ledger.db: schema constraints that protect correctness."""

import sqlite3

import pytest

from ledger.db import SCHEMA_VERSION, init_schema, transaction


def add_file(conn, sha="abc", kind="events"):
    cur = conn.execute(
        "INSERT INTO ingested_files (sha256, path, kind, ingested_at) VALUES (?, ?, ?, ?)",
        (sha, f"{sha}.jsonl", kind, "2026-11-13T00:00:00Z"),
    )
    return cur.lastrowid


def add_lead(conn, phone_hash="h1", workflow_id="motor_renewal"):
    conn.execute(
        "INSERT INTO leads (workflow_id, phone_hash, phone_last4, pii_enc,"
        " external_ref, batch_id, attributes) VALUES (?, ?, '3210', 'enc', 'REF', 'batch_001', '{}')",
        (workflow_id, phone_hash),
    )


def add_event(conn, file_id, event_id="evt_1"):
    conn.execute(
        "INSERT INTO events (event_id, file_id, event_type, payload) VALUES (?, ?, 'call.initiated', '{}')"
        " ON CONFLICT (event_id) DO NOTHING",
        (event_id, file_id),
    )


def table_names(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_init_schema_creates_tables_and_version(conn):
    assert {"ingested_files", "lead_rows", "leads", "events"} <= table_names(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_init_schema_twice_is_a_no_op(conn):
    add_file(conn)
    init_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 1


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_event(conn, file_id=999)


def test_lead_identity_is_unique_per_workflow(conn):
    add_lead(conn, "h1", "motor_renewal")
    add_lead(conn, "h1", "policy_feedback")  # same phone, other workflow: allowed
    with pytest.raises(sqlite3.IntegrityError):
        add_lead(conn, "h1", "motor_renewal")


def test_same_file_hash_cannot_be_recorded_twice(conn):
    add_file(conn, sha="same")
    with pytest.raises(sqlite3.IntegrityError):
        add_file(conn, sha="same")


def test_duplicate_event_id_is_ignored(conn):
    file_id = add_file(conn)
    add_event(conn, file_id, "evt_1")
    add_event(conn, file_id, "evt_1")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("UPDATE events SET event_type = 'x'", id="update"),
        pytest.param("DELETE FROM events", id="delete"),
    ],
)
def test_events_ledger_is_append_only(conn, statement):
    add_event(conn, add_file(conn))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(statement)


def test_rejected_lead_row_requires_a_reason(conn):
    file_id = add_file(conn, kind="leads")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lead_rows (file_id, row_number, workflow_id, batch_id, outcome)"
            " VALUES (?, 1, 'motor_renewal', 'batch_001', 'rejected')",
            (file_id,),
        )


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            add_file(conn)
            raise RuntimeError("crash mid-file")
    assert conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 0


def test_transaction_commits_on_success(conn):
    with transaction(conn):
        add_file(conn)
    assert conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 1
