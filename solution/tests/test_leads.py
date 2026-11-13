"""Tests for ledger.ingest_leads."""
import json
import re

import pytest

from ledger.config import SCHEMAS_DIR
from ledger.db import IngestError
from ledger.ingest_leads import ingest_lead_file, ingest_leads, validate_row
from ledger.schema import load_schemas

SCHEMAS = load_schemas(SCHEMAS_DIR)
MOTOR = SCHEMAS["motor_renewal"]
HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language"


def row(**overrides):
    base = {
        "customer_name": "Ananya Iyer",
        "phone": "9876543210",
        "policy_number": "MOT/2026/100001",
        "policy_expiry": "01/12/2026",
        "premium_amount": "₹12,345",
        "preferred_language": "HI",
    }
    return {**base, **overrides}


def write_csv(path, *lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def lead_rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT batch_id, row_number, outcome, reason, field_warnings FROM lead_rows"
        " ORDER BY file_id, row_number"
    )]


def only_lead(conn, pii):
    (lead,) = conn.execute("SELECT * FROM leads").fetchall()
    return {
        "batch_id": lead["batch_id"],
        "external_ref": lead["external_ref"],
        **json.loads(pii.decrypt(lead["pii_enc"])),
        **json.loads(lead["attributes"]),
    }


# ---------- validate_row ----------

def test_valid_row_is_normalised():
    result = validate_row(MOTOR, row())
    assert result.reason is None
    assert result.warnings == 0
    assert result.values == {
        "customer_name": "Ananya Iyer",
        "phone": "+919876543210",
        "policy_number": "MOT/2026/100001",
        "policy_expiry": "2026-12-01",
        "premium_amount": "12345.00",
        "preferred_language": "hi",
    }


@pytest.mark.parametrize(
    "overrides, reason",
    [
        pytest.param({"customer_name": "   "}, "missing_required:customer_name", id="blank-name"),
        pytest.param({"phone": "N/A"}, "invalid_phone", id="bad-phone"),
        pytest.param({"phone": ""}, "missing_required:phone", id="missing-phone"),
        pytest.param({"policy_number": ""}, "missing_required:policy_number", id="missing-ref"),
        pytest.param(
            {"customer_name": "", "phone": "12345"},
            "missing_required:customer_name",
            id="schema-order-decides",
        ),
    ],
)
def test_rejection_reason(overrides, reason):
    assert validate_row(MOTOR, row(**overrides)).reason == reason


@pytest.mark.parametrize(
    "overrides, field",
    [
        pytest.param({"policy_expiry": "31/02/2026"}, "policy_expiry", id="bad-date"),
        pytest.param({"premium_amount": "twelve thousand"}, "premium_amount", id="bad-amount"),
        pytest.param({"preferred_language": "Hindi"}, "preferred_language", id="bad-enum"),
    ],
)
def test_invalid_optional_field_is_null_with_one_warning(overrides, field):
    result = validate_row(MOTOR, row(**overrides))
    assert result.reason is None
    assert result.values[field] is None
    assert result.warnings == 1


def test_empty_optional_field_is_not_a_warning():
    result = validate_row(MOTOR, row(preferred_language="", policy_expiry=""))
    assert result.warnings == 0
    assert result.values["preferred_language"] is None


def test_rejected_row_has_no_warnings():
    result = validate_row(MOTOR, row(phone="N/A", policy_expiry="soon"))
    assert result.reason == "invalid_phone"
    assert result.warnings == 0


# ---------- ingest_lead_file ----------

def test_blank_rows_are_skipped_and_not_numbered(conn, pii, tmp_path):
    path = write_csv(
        tmp_path / "batch_001.csv",
        HEADER,
        "A One,9876543210,REF1,,,en",
        ",,,,,",
        "  , ,,,,",
        "B Two,N/A,REF2,,,en",
    )
    result = ingest_lead_file(conn, pii, MOTOR, path)
    assert (result.created, result.rejected) == (1, 1)
    assert lead_rows(conn) == [
        {"batch_id": "batch_001", "row_number": 1, "outcome": "created", "reason": None, "field_warnings": 0},
        {"batch_id": "batch_001", "row_number": 2, "outcome": "rejected", "reason": "invalid_phone", "field_warnings": 0},
    ]


def test_duplicate_merges_non_null_values_and_keeps_first_batch(conn, pii, tmp_path):
    first = write_csv(tmp_path / "batch_001.csv", HEADER, "Ananya Iyer,9876543210,REF1,01/12/2026,1000,ta")
    # Same phone in another format; empty expiry must not wipe the stored one.
    second = write_csv(tmp_path / "batch_002.csv", HEADER, "Ananya I,+91 98765 43210,REF1,,Rs. 2000,")
    ingest_lead_file(conn, pii, MOTOR, first)
    result = ingest_lead_file(conn, pii, MOTOR, second)

    assert result.merged == 1
    assert only_lead(conn, pii) == {
        "batch_id": "batch_001",
        "external_ref": "REF1",
        "phone": "+919876543210",
        "customer_name": "Ananya I",
        "policy_expiry": "2026-12-01",
        "premium_amount": "2000.00",
        "preferred_language": "ta",
    }


def test_duplicate_within_the_same_file_is_merged(conn, pii, tmp_path):
    path = write_csv(
        tmp_path / "batch_001.csv",
        HEADER,
        "A One,9876543210,REF1,,,en",
        "A One,09876543210,REF1,,,hi",
    )
    result = ingest_lead_file(conn, pii, MOTOR, path)
    assert (result.created, result.merged) == (1, 1)
    assert only_lead(conn, pii)["preferred_language"] == "hi"


def test_invalid_optional_value_in_duplicate_does_not_overwrite(conn, pii, tmp_path):
    first = write_csv(tmp_path / "batch_001.csv", HEADER, "A One,9876543210,REF1,01/12/2026,,en")
    second = write_csv(tmp_path / "batch_002.csv", HEADER, "A One,9876543210,REF1,TBD,,en")
    ingest_lead_file(conn, pii, MOTOR, first)
    ingest_lead_file(conn, pii, MOTOR, second)
    assert only_lead(conn, pii)["policy_expiry"] == "2026-12-01"


def test_rejected_row_then_valid_row_creates_lead_in_later_batch(conn, pii, tmp_path):
    first = write_csv(tmp_path / "batch_001.csv", HEADER, ",9876543210,REF1,,,en")
    second = write_csv(tmp_path / "batch_002.csv", HEADER, "A One,9876543210,REF1,,,en")
    ingest_lead_file(conn, pii, MOTOR, first)
    ingest_lead_file(conn, pii, MOTOR, second)
    assert only_lead(conn, pii)["batch_id"] == "batch_002"


def test_reingesting_a_file_changes_nothing(conn, pii, tmp_path):
    path = write_csv(tmp_path / "batch_001.csv", HEADER, "A One,9876543210,REF1,,,en")
    ingest_lead_file(conn, pii, MOTOR, path)
    before = lead_rows(conn)

    result = ingest_lead_file(conn, pii, MOTOR, path)
    assert result.skipped
    assert lead_rows(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 1


def test_alias_headers_and_extra_columns(conn, pii, tmp_path):
    path = write_csv(
        tmp_path / "batch_003.csv",
        "Language,Insured Name,Policy No,Mobile No,Expiry Date,Premium,agent_notes",
        "EN,A One,REF1,(+91) 9876543210,01-Dec-2026,INR 500.5,VIP",
    )
    ingest_lead_file(conn, pii, MOTOR, path)
    lead = only_lead(conn, pii)
    assert lead["policy_expiry"] == "2026-12-01"
    assert lead["premium_amount"] == "500.50"
    assert "agent_notes" not in lead


def test_short_row_counts_missing_cells_as_empty(conn, pii, tmp_path):
    path = write_csv(tmp_path / "batch_001.csv", HEADER, "A One,9876543210")
    ingest_lead_file(conn, pii, MOTOR, path)
    assert lead_rows(conn)[0]["reason"] == "missing_required:policy_number"


def test_no_pii_is_stored_in_plaintext(conn, pii, tmp_path):
    write_csv(tmp_path / "leads" / "motor_renewal" / "batch_001.csv",
              HEADER, "Ananya Iyer,9876543210,REF1,,,en", "Rohan Rao,N/A,REF2,,,en")
    ingest_leads(conn, pii, SCHEMAS, tmp_path)

    stored = " ".join(
        str(value)
        for table in ("leads", "lead_rows")
        for record in conn.execute(f"SELECT * FROM {table}")
        for value in tuple(record)
    )
    assert "Ananya" not in stored and "Rohan" not in stored
    assert not re.search(r"\d{10}", stored)


def test_ingest_leads_walks_workflows_and_skips_unknown(conn, pii, tmp_path, capsys):
    write_csv(tmp_path / "leads" / "motor_renewal" / "batch_001.csv", HEADER, "A,9876543210,R1,,,en")
    write_csv(
        tmp_path / "leads" / "policy_feedback" / "batch_001.csv",
        "customer_name,phone,order_id,product,order_date,preferred_language",
        "B,9876543210,ORD-1,Phone,,en",
    )
    write_csv(tmp_path / "leads" / "mystery" / "batch_001.csv", "x", "1")

    results = ingest_leads(conn, pii, SCHEMAS, tmp_path)
    assert [r.created for r in results] == [1, 1]  # same phone, different workflows
    assert "mystery" in capsys.readouterr().out


def test_existing_lead_merged_twice_in_a_later_file(conn, pii, tmp_path):
    first = write_csv(tmp_path / "batch_001.csv", HEADER, "A One,9876543210,REF1,01/12/2026,100,en")
    second = write_csv(
        tmp_path / "batch_002.csv",
        HEADER,
        "A Two,9876543210,REF1,,200,",
        "A Three,09876543210,REF1,,,mr",
    )
    ingest_lead_file(conn, pii, MOTOR, first)
    result = ingest_lead_file(conn, pii, MOTOR, second)

    assert (result.created, result.merged) == (0, 2)
    assert only_lead(conn, pii) == {
        "batch_id": "batch_001",
        "external_ref": "REF1",
        "phone": "+919876543210",
        "customer_name": "A Three",
        "policy_expiry": "2026-12-01",
        "premium_amount": "200.00",
        "preferred_language": "mr",
    }


# ---------- file-level errors ----------

def counts(conn):
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("ingested_files", "lead_rows", "leads")
    )


def test_non_utf8_csv_is_refused_and_nothing_is_kept(conn, pii, tmp_path):
    path = tmp_path / "batch_001.csv"
    path.write_bytes((HEADER + "\nJosé Rao,9876543210,REF1,,,en\n").encode("cp1252"))
    with pytest.raises(IngestError, match="not a readable UTF-8 CSV"):
        ingest_lead_file(conn, pii, MOTOR, path)
    assert counts(conn) == (0, 0, 0)


def test_changed_file_with_the_same_name_is_refused(conn, pii, tmp_path):
    path = write_csv(tmp_path / "drop_1" / "leads" / "motor_renewal" / "batch_001.csv",
                     HEADER, "A One,9876543210,REF1,,,en")
    ingest_lead_file(conn, pii, MOTOR, path)
    before = counts(conn)

    write_csv(path, HEADER, "A One,9876543210,REF1,,,en", "B Two,9123456789,REF2,,,en")
    with pytest.raises(IngestError, match="different content"):
        ingest_lead_file(conn, pii, MOTOR, path)
    assert counts(conn) == before


def test_same_content_in_a_moved_folder_is_skipped(conn, pii, tmp_path):
    original = write_csv(tmp_path / "a" / "batch_001.csv", HEADER, "A One,9876543210,REF1,,,en")
    moved = write_csv(tmp_path / "b" / "batch_001.csv", HEADER, "A One,9876543210,REF1,,,en")
    ingest_lead_file(conn, pii, MOTOR, original)
    assert ingest_lead_file(conn, pii, MOTOR, moved).skipped
