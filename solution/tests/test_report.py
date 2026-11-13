"""Tests for ledger.report formatting and data_quality on a small database."""
import json

import pytest

from ledger.config import DISPOSITIONS_CSV, SCHEMAS_DIR
from ledger.ingest_events import ingest_event_file
from ledger.ingest_leads import ingest_lead_file
from ledger.report import format_bool, format_rate, write_reports
from ledger.schema import load_dispositions, load_schemas

CATALOGUE = load_dispositions(DISPOSITIONS_CSV)
MOTOR = load_schemas(SCHEMAS_DIR)["motor_renewal"]


@pytest.mark.parametrize(
    "connected, total, expected",
    [(9, 16, "0.5625"), (1, 1, "1.0000"), (0, 3, "0.0000"), (2, 3, "0.6667")],
)
def test_format_rate_has_exactly_four_decimals(connected, total, expected):
    assert format_rate(connected, total) == expected


def test_format_bool():
    assert (format_bool(True), format_bool(False)) == ("true", "false")


@pytest.fixture
def reports(conn, pii, tmp_path):
    leads = tmp_path / "batch_001.csv"
    leads.write_text(
        "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"
        "Ananya Iyer,9876543210,REF2,TBD,,en\n"
        ",,,,,\n"
        "Rohan Rao,N/A,REF9,,,en\n"
        "Meera Nair,+91 91234 56789,REF1,,,en\n"
        "Meera N,09123456789,REF1,,,\n",
        encoding="utf-8",
    )
    events = tmp_path / "events_0001.jsonl"
    lines = [
        {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "c1", "occurred_at": "2026-10-05T04:30:00Z", "phone": "9876543210", "payload": {}},
        {"event_id": "e2", "event_type": "call.connected", "workflow_id": "motor_renewal",
         "call_id": "c1", "occurred_at": "2026-10-05T04:30:09Z", "phone": "9876543210", "payload": {}},
        {"event_id": "e3", "event_type": "disposition.set", "workflow_id": "motor_renewal",
         "call_id": "c1", "occurred_at": "2026-10-05T04:35:00Z", "phone": "9876543210",
         "payload": {"disposition_code": "renewed"}},
        {"event_id": "e4", "event_type": "disposition.set", "workflow_id": "motor_renewal",
         "call_id": "c1", "occurred_at": "2026-10-05T04:36:00Z", "phone": "9876543210",
         "payload": {"disposition_code": "RENEWD"}},
        {"event_id": "e5", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "c9", "occurred_at": "2026-10-05T05:00:00Z", "phone": "9000000000", "payload": {}},
    ]
    events.write_text(
        "\n".join([*(json.dumps(x) for x in lines), json.dumps(lines[0]), '{"event_id": "e6", "event'])
        + "\n",
        encoding="utf-8",
    )
    ingest_lead_file(conn, pii, MOTOR, leads)
    ingest_event_file(conn, pii, events)
    out = tmp_path / "reports"
    write_reports(conn, CATALOGUE, out)
    return out


def read(path):
    return path.read_text(encoding="utf-8")


def test_lead_report(reports):
    assert read(reports / "lead_report.csv") == (
        "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,"
        "last_call_at,current_disposition,ever_success\n"
        "motor_renewal,REF1,batch_001,+91******6789,yet_to_reach_out,0,0,,,false\n"
        "motor_renewal,REF2,batch_001,+91******3210,completed,1,1,2026-10-05T10:00:00+05:30,renewed,true\n"
    )


def test_batch_summary(reports):
    assert read(reports / "batch_summary.csv") == (
        "workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,"
        "in_progress_dialed,completed\n"
        "motor_renewal,batch_001,2,1,0,0,1\n"
    )


def test_daily_overview(reports):
    assert read(reports / "daily_overview.csv") == (
        "workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads\n"
        "motor_renewal,2026-10-05,1,1,1.0000,1\n"
    )


def test_rejections_have_no_pii(reports):
    content = read(reports / "rejections.csv")
    assert content == "workflow_id,batch_id,row_number,reason\nmotor_renewal,batch_001,2,invalid_phone\n"
    assert "Rohan" not in content


def test_data_quality(reports):
    assert json.loads(read(reports / "data_quality.json")) == {
        "lead_rows_total": 4,
        "lead_rows_rejected": 1,
        "rejections_by_reason": {"invalid_phone": 1},
        "duplicate_lead_rows_merged": 1,
        "field_warnings": 1,
        "events_total": 7,
        "malformed_events": 1,
        "duplicate_events_dropped": 1,
        "orphan_events": 1,
        "unknown_disposition_events": 1,
    }


def test_data_quality_key_order(reports):
    keys = list(json.loads(read(reports / "data_quality.json")))
    assert keys == [
        "lead_rows_total", "lead_rows_rejected", "rejections_by_reason", "duplicate_lead_rows_merged",
        "field_warnings", "events_total", "malformed_events", "duplicate_events_dropped",
        "orphan_events", "unknown_disposition_events",
    ]


def test_empty_database_still_writes_all_files(conn, tmp_path):
    write_reports(conn, CATALOGUE, tmp_path / "out")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "batch_summary.csv", "daily_overview.csv", "data_quality.json",
        "lead_report.csv", "rejections.csv",
    ]
    assert json.loads(read(tmp_path / "out" / "data_quality.json"))["events_total"] == 0
