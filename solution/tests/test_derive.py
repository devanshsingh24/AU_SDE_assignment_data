"""Tests for ledger.derive: the spec's derived-fact rules."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from ledger.config import DISPOSITIONS_CSV, SCHEMAS_DIR
from ledger.db import connect, init_schema
from ledger.derive import (
    Bucket,
    Call,
    Event,
    EventType,
    Lead,
    bucket_for,
    build_calls,
    daily_overview,
    derive,
    effective_disposition,
    link_events,
    summarize_lead,
)
from ledger.ingest_events import ingest_event_file
from ledger.ingest_leads import ingest_lead_file
from ledger.normalize import IST
from ledger.schema import load_dispositions, load_schemas

CATALOGUE = load_dispositions(DISPOSITIONS_CSV)
MOTOR = load_schemas(SCHEMAS_DIR)["motor_renewal"]
MR = "motor_renewal"
T0 = datetime(2026, 10, 5, 10, 0, tzinfo=IST)


def ev(event_id, event_type, *, call_id="c1", minutes=0, code=None, phone_hash="h1", workflow_id=MR):
    return Event(event_id, event_type, workflow_id, call_id,
                 T0 + timedelta(minutes=minutes), phone_hash, code)


def call(call_id="c1", *, lead_id=1, minutes=0, connected=False, failed=False, disposition=None):
    return Call(call_id, lead_id, MR, T0 + timedelta(minutes=minutes), connected, failed, disposition)


SET, OVERRIDE = EventType.DISPOSITION_SET, EventType.DISPOSITION_OVERRIDE


# ---------- effective_disposition ----------

@pytest.mark.parametrize(
    "events, expected",
    [
        pytest.param([], None, id="none"),
        pytest.param([ev("e1", SET, code="callback_requested", minutes=1),
                      ev("e2", SET, code="not_interested", minutes=2)],
                     "not_interested", id="latest-set"),
        pytest.param([ev("e1", OVERRIDE, code="renewed", minutes=1),
                      ev("e2", SET, code="not_interested", minutes=9)],
                     "renewed", id="override-beats-later-set"),
        pytest.param([ev("e1", OVERRIDE, code="renewed", minutes=5),
                      ev("e2", OVERRIDE, code="dnd", minutes=3)],
                     "renewed", id="latest-override"),
        pytest.param([ev("e1", OVERRIDE, code="renewed"),
                      ev("e2", OVERRIDE, code="dnd")],
                     "dnd", id="tie-broken-by-event-id"),
        pytest.param([ev("e1", OVERRIDE, code="renewed")], "renewed", id="override-only"),
    ],
)
def test_effective_disposition(events, expected):
    assert effective_disposition(events) == expected


# ---------- bucket_for ----------

@pytest.mark.parametrize(
    "calls, expected",
    [
        pytest.param([], Bucket.YET_TO_REACH_OUT, id="no-calls"),
        pytest.param([call(disposition="renewed")], Bucket.COMPLETED, id="terminal-success"),
        pytest.param([call(disposition="not_interested")], Bucket.COMPLETED, id="terminal-not-success"),
        pytest.param([call("c1", failed=True), call("c2", disposition="dnd")],
                     Bucket.COMPLETED, id="completed-beats-failed"),
        pytest.param([call("c1", failed=True), call("c2", failed=True)],
                     Bucket.TELEPHONY_FAILED, id="all-failed"),
        pytest.param([call("c1", failed=True), call("c2")], Bucket.DIALED, id="some-failed"),
        pytest.param([call(connected=True, disposition="callback_requested")],
                     Bucket.DIALED, id="non-terminal"),
    ],
)
def test_bucket_for(calls, expected):
    assert bucket_for(calls, CATALOGUE) == expected


# ---------- link_events ----------

LEADS = {(MR, "h1"): 1}


def test_links_by_workflow_and_phone():
    links, orphans = link_events([ev("e1", EventType.INITIATED)], LEADS)
    assert links == {"e1": 1} and orphans == 0


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(ev("e1", EventType.INITIATED, workflow_id="policy_feedback"), id="other-workflow"),
        pytest.param(ev("e1", EventType.INITIATED, phone_hash=None), id="invalid-phone"),
        pytest.param(ev("e1", EventType.INITIATED, phone_hash="nobody"), id="never-uploaded"),
        pytest.param(ev("e1", OVERRIDE, phone_hash=None, call_id="unknown"), id="override-unknown-call"),
    ],
)
def test_orphans(event):
    links, orphans = link_events([event], LEADS)
    assert links == {} and orphans == 1


def test_override_links_through_call_id():
    events = [ev("e1", EventType.INITIATED), ev("e2", OVERRIDE, phone_hash=None, code="renewed")]
    links, orphans = link_events(events, LEADS)
    assert links == {"e1": 1, "e2": 1} and orphans == 0


# ---------- build_calls ----------

def lead_of(events):
    return [(e, 1) for e in events]


def test_call_without_initiated_is_not_counted():
    calls, _ = build_calls(lead_of([ev("e1", EventType.CONNECTED)]), {1: MR}, CATALOGUE)
    assert calls == []


def test_unknown_code_is_counted_and_ignored():
    events = [
        ev("e1", EventType.INITIATED),
        ev("e2", SET, code="payment_link_sent", minutes=1),
        ev("e3", SET, code="RENEWD", minutes=2),
        ev("e4", OVERRIDE, code="unknown", minutes=3),
    ]
    calls, unknown = build_calls(lead_of(events), {1: MR}, CATALOGUE)
    assert unknown == 2
    assert calls[0].disposition == "payment_link_sent"


def test_code_valid_in_another_workflow_is_unknown():
    events = [ev("e1", EventType.INITIATED), ev("e2", SET, code="feedback_captured")]
    _, unknown = build_calls(lead_of(events), {1: MR}, CATALOGUE)
    assert unknown == 1


def test_call_flags():
    events = [
        ev("e1", EventType.INITIATED, call_id="a"),
        ev("e2", EventType.CONNECTED, call_id="a"),
        ev("e3", EventType.INITIATED, call_id="b"),
        ev("e4", EventType.TELEPHONY_FAILED, call_id="b"),
    ]
    calls, _ = build_calls(lead_of(events), {1: MR}, CATALOGUE)
    assert [(c.call_id, c.connected, c.telephony_failed) for c in calls] == [
        ("a", True, False),
        ("b", False, True),
    ]


# ---------- summarize_lead ----------

LEAD = Lead(1, MR, "REF1", "batch_001", "3210")


def test_lead_summary():
    calls = [
        call("c1", minutes=0, connected=True, disposition="renewed"),
        call("c2", minutes=60, connected=True, disposition="callback_requested"),
        call("c3", minutes=120),  # latest call, but no disposition
    ]
    s = summarize_lead(LEAD, calls, CATALOGUE)
    assert (s.bucket, s.total_calls, s.connected_calls) == (Bucket.COMPLETED, 3, 2)
    assert s.current_disposition == "callback_requested"
    assert s.ever_success is True
    assert s.last_call_at == "2026-10-05T12:00:00+05:30"


def test_lead_without_calls():
    s = summarize_lead(LEAD, [], CATALOGUE)
    assert (s.bucket, s.last_call_at, s.current_disposition, s.ever_success) == (
        Bucket.YET_TO_REACH_OUT, "", "", False,
    )


# ---------- daily_overview ----------

def test_daily_overview_uses_ist_dates_and_distinct_success_leads():
    late_utc = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)  # 01:30 IST on 6 Oct
    calls = [
        call("c1", lead_id=1, connected=True, disposition="renewed"),
        call("c2", lead_id=1, minutes=30, connected=True, disposition="renewed"),
        call("c3", lead_id=2),
        Call("c4", 3, MR, late_utc, True, False, "renewed"),
    ]
    rows = daily_overview(calls, CATALOGUE)
    assert [(r.date, r.total_calls, r.connected_calls, r.success_leads) for r in rows] == [
        ("2026-10-05", 3, 2, 1),
        ("2026-10-06", 1, 1, 1),
    ]


# ---------- integration with the database ----------

HEADER = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language"


def jsonl(path, *events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def leads_csv(path, *rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
    return path


def e(event_id, event_type, call_id, occurred_at, payload=None, phone="+91 98765 43210"):
    out = {"event_id": event_id, "event_type": event_type, "workflow_id": MR,
           "call_id": call_id, "occurred_at": occurred_at, "payload": payload or {}}
    if phone is not None:
        out["phone"] = phone
    return out


EVENTS = [
    e("evt_1", "call.initiated", "call_1", "2026-10-05T04:30:00Z"),
    e("evt_2", "call.connected", "call_1", "2026-10-05T04:30:10Z"),
    e("evt_3", "disposition.set", "call_1", "2026-10-05 10:05:00", {"disposition_code": "callback_requested"}),
    e("evt_4", "call.initiated", "call_2", "2026-10-06T11:00:00+05:30", phone="09876543210"),
    e("evt_5", "call.connected", "call_2", "2026-10-06T05:30:20Z"),
    e("evt_6", "disposition.set", "call_2", "2026-10-06T05:35:00Z", {"disposition_code": "not_interested"}),
    e("evt_7", "disposition.override", "call_2", "2026-10-07T09:00:00Z",
      {"disposition_code": "renewed", "overridden_by": "supervisor_1", "reason": "qa_review"}, phone=None),
    e("evt_8", "call.initiated", "call_9", "2026-10-05T05:00:00Z", phone="9000000000"),  # never uploaded
]


def test_end_to_end_lead_summary(conn, pii, tmp_path):
    ingest_lead_file(conn, pii, MOTOR, leads_csv(tmp_path / "batch_001.csv", "A One,9876543210,REF1,,,en"))
    ingest_event_file(conn, pii, jsonl(tmp_path / "events_0001.jsonl", *EVENTS))
    d = derive(conn, CATALOGUE)

    (lead,) = d.leads
    assert lead.bucket == Bucket.COMPLETED
    assert (lead.total_calls, lead.connected_calls) == (2, 2)
    assert lead.current_disposition == "renewed"
    assert lead.ever_success is True
    assert lead.last_call_at == "2026-10-06T11:00:00+05:30"
    assert (d.orphan_events, d.unknown_disposition_events) == (1, 0)
    assert [(r.date, r.total_calls, r.success_leads) for r in d.daily] == [
        ("2026-10-05", 1, 0),
        ("2026-10-06", 1, 1),  # the override on 7 Oct counts on the call's date
    ]


def test_event_before_its_lead_is_linked_once_the_lead_arrives(conn, pii, tmp_path):
    ingest_event_file(conn, pii, jsonl(tmp_path / "drop_1" / "events_0001.jsonl", *EVENTS))
    assert derive(conn, CATALOGUE).orphan_events == len(EVENTS)

    ingest_lead_file(conn, pii, MOTOR, leads_csv(tmp_path / "drop_2" / "batch_003.csv", "A One,9876543210,REF1,,,en"))
    after = derive(conn, CATALOGUE)
    assert after.orphan_events == 1
    assert after.leads[0].total_calls == 2


def test_ingest_order_does_not_change_the_result(pii, tmp_path):
    leads = leads_csv(tmp_path / "batch_001.csv", "A One,9876543210,REF1,,,en", "B Two,9123456789,REF2,,,hi")
    events = jsonl(tmp_path / "events_0001.jsonl", *EVENTS)

    results = []
    for name, order in (("leads_first", (leads, events)), ("events_first", (events, leads))):
        conn = connect(str(tmp_path / f"{name}.db"))
        init_schema(conn)
        for path in order:
            if path.suffix == ".csv":
                ingest_lead_file(conn, pii, MOTOR, path)
            else:
                ingest_event_file(conn, pii, path)
        results.append(derive(conn, CATALOGUE))
        conn.close()
    assert results[0] == results[1]
