"""Derived facts: linking, calls, effective dispositions, buckets, daily overview.

Spec: Assignment A › Event files (steps 3-4) and › Derived facts.

Everything here is computed from the *current* database state each time a
report is built, never stored. That is what makes an orphan from drop_1 count
once its lead arrives in drop_2, and makes ingesting drop by drop give the
same reports as ingesting everything at once.

The pure functions take plain dataclasses, so each spec rule can be tested
without a database. ``derive()`` at the bottom loads the data and runs them.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from ledger.normalize import to_ist
from ledger.schema import Catalogue


class EventType(StrEnum):
    INITIATED = "call.initiated"
    TELEPHONY_FAILED = "call.telephony_failed"
    CONNECTED = "call.connected"
    ENDED = "call.ended"
    DISPOSITION_SET = "disposition.set"
    DISPOSITION_OVERRIDE = "disposition.override"


DISPOSITION_TYPES = {EventType.DISPOSITION_SET, EventType.DISPOSITION_OVERRIDE}


class Bucket(StrEnum):
    YET_TO_REACH_OUT = "yet_to_reach_out"
    COMPLETED = "completed"
    TELEPHONY_FAILED = "in_progress_telephony_failed"
    DIALED = "in_progress_dialed"


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# Not frozen: frozen dataclasses build every object through object.__setattr__,
# which is noticeably slower for the ~600k events of the large dataset.
# Treat it as read-only all the same.
@dataclass(slots=True)
class Event:
    event_id: str
    event_type: str | None
    workflow_id: str | None
    call_id: str | None
    occurred_at: datetime | None
    phone_hash: str | None
    code: str | None = None  # disposition_code, for disposition.* events

    def sort_key(self) -> tuple:
        """Chronological order; ties broken by event_id; unknown times first."""
        return (self.occurred_at is not None, self.occurred_at or _EPOCH, self.event_id)


@dataclass(frozen=True)
class Lead:
    lead_id: int
    workflow_id: str
    external_ref: str
    batch_id: str
    phone_last4: str


@dataclass(frozen=True)
class Call:
    call_id: str
    lead_id: int
    workflow_id: str
    initiated_at: datetime
    connected: bool
    telephony_failed: bool
    disposition: str | None  # effective disposition


@dataclass(frozen=True)
class LeadSummary:
    workflow_id: str
    external_ref: str
    batch_id: str
    phone_last4: str
    bucket: Bucket
    total_calls: int
    connected_calls: int
    last_call_at: str  # ISO-8601 in IST, "" if no calls
    current_disposition: str  # "" if none
    ever_success: bool


@dataclass(frozen=True)
class DailyRow:
    workflow_id: str
    date: str  # IST date, YYYY-MM-DD
    total_calls: int
    connected_calls: int
    success_leads: int


@dataclass(frozen=True)
class Derivation:
    leads: list[LeadSummary]
    daily: list[DailyRow]
    orphan_events: int
    unknown_disposition_events: int


# ---------- pure rules ----------

def link_events(
    events: list[Event], lead_ids: dict[tuple[str, str], int]
) -> tuple[dict[str, int], int]:
    """Map event_id → lead_id. Returns the links and the number of orphans.

    Events link via (workflow_id, phone_hash). Overrides have no phone, so they
    link through their call_id to the lead of that call's other events.
    """
    links: dict[str, int] = {}
    for e in events:
        if e.event_type != EventType.DISPOSITION_OVERRIDE and e.phone_hash is not None:
            lead_id = lead_ids.get((e.workflow_id, e.phone_hash))
            if lead_id is not None:
                links[e.event_id] = lead_id

    # The call's lead comes from its call.initiated if linked, else its earliest
    # linked event. One pass keeping the best candidate per call (no full sort).
    best: dict[str, tuple[tuple, int]] = {}
    for e in events:
        if e.call_id is None or e.event_id not in links:
            continue
        rank = (e.event_type != EventType.INITIATED, e.sort_key())
        current = best.get(e.call_id)
        if current is None or rank < current[0]:
            best[e.call_id] = (rank, links[e.event_id])
    call_lead = {call_id: lead_id for call_id, (_, lead_id) in best.items()}

    for e in events:
        if e.event_type == EventType.DISPOSITION_OVERRIDE and e.call_id in call_lead:
            links[e.event_id] = call_lead[e.call_id]

    return links, len(events) - len(links)


def effective_disposition(disposition_events: list[Event]) -> str | None:
    """Latest override by occurred_at, else latest set, else None.

    Expects only events whose code is in the catalogue.
    """
    overrides = [e for e in disposition_events if e.event_type == EventType.DISPOSITION_OVERRIDE]
    candidates = overrides or [
        e for e in disposition_events if e.event_type == EventType.DISPOSITION_SET
    ]
    return max(candidates, key=Event.sort_key).code if candidates else None


def build_calls(
    linked: list[tuple[Event, int]], lead_workflow: dict[int, str], catalogue: Catalogue
) -> tuple[list[Call], int]:
    """Group linked events into calls. Returns the calls and the unknown-disposition count.

    A call counts only if it has a call.initiated event (with a usable time).
    Disposition events whose code is not in the catalogue for the lead's
    workflow are counted and ignored.
    """
    unknown = 0
    by_call: dict[str, list[tuple[Event, int]]] = defaultdict(list)
    for e, lead_id in linked:
        if e.event_type in DISPOSITION_TYPES and (lead_workflow[lead_id], e.code) not in catalogue:
            unknown += 1
            continue
        if e.call_id is not None:
            by_call[e.call_id].append((e, lead_id))

    calls = []
    for call_id in sorted(by_call):
        items = by_call[call_id]
        initiated = [
            (e, lead_id)
            for e, lead_id in items
            if e.event_type == EventType.INITIATED and e.occurred_at is not None
        ]
        if not initiated:
            continue
        first, lead_id = min(initiated, key=lambda pair: pair[0].sort_key())
        types = {e.event_type for e, _ in items}
        calls.append(
            Call(
                call_id=call_id,
                lead_id=lead_id,
                workflow_id=lead_workflow[lead_id],
                initiated_at=first.occurred_at,
                connected=EventType.CONNECTED in types,
                telephony_failed=EventType.TELEPHONY_FAILED in types,
                disposition=effective_disposition(
                    [e for e, _ in items if e.event_type in DISPOSITION_TYPES]
                ),
            )
        )
    return calls, unknown


def _is_terminal(call: Call, catalogue: Catalogue) -> bool:
    return call.disposition is not None and catalogue[(call.workflow_id, call.disposition)].is_terminal


def _is_success(call: Call, catalogue: Catalogue) -> bool:
    return call.disposition is not None and catalogue[(call.workflow_id, call.disposition)].is_success


def bucket_for(calls: list[Call], catalogue: Catalogue) -> Bucket:
    """The first matching bucket, in the spec's order."""
    if not calls:
        return Bucket.YET_TO_REACH_OUT
    if any(_is_terminal(c, catalogue) for c in calls):
        return Bucket.COMPLETED
    if all(c.telephony_failed for c in calls):
        return Bucket.TELEPHONY_FAILED
    return Bucket.DIALED


def summarize_lead(lead: Lead, calls: list[Call], catalogue: Catalogue) -> LeadSummary:
    by_time = sorted(calls, key=lambda c: (c.initiated_at, c.call_id))
    with_disposition = [c for c in by_time if c.disposition is not None]
    return LeadSummary(
        workflow_id=lead.workflow_id,
        external_ref=lead.external_ref,
        batch_id=lead.batch_id,
        phone_last4=lead.phone_last4,
        bucket=bucket_for(calls, catalogue),
        total_calls=len(calls),
        connected_calls=sum(c.connected for c in calls),
        last_call_at=to_ist(by_time[-1].initiated_at).isoformat() if by_time else "",
        current_disposition=with_disposition[-1].disposition if with_disposition else "",
        ever_success=any(_is_success(c, catalogue) for c in calls),
    )


def daily_overview(calls: list[Call], catalogue: Catalogue) -> list[DailyRow]:
    """One row per (workflow, IST date of call.initiated) that has calls."""
    groups: dict[tuple[str, str], list[Call]] = defaultdict(list)
    for c in calls:
        groups[(c.workflow_id, to_ist(c.initiated_at).date().isoformat())].append(c)
    return [
        DailyRow(
            workflow_id=workflow_id,
            date=day,
            total_calls=len(day_calls),
            connected_calls=sum(c.connected for c in day_calls),
            # Distinct leads; a later override still counts on the call's own date.
            success_leads=len({c.lead_id for c in day_calls if _is_success(c, catalogue)}),
        )
        for (workflow_id, day), day_calls in sorted(groups.items())
    ]


# ---------- loading from the database ----------

def derive(conn: sqlite3.Connection, catalogue: Catalogue) -> Derivation:
    """Run every rule over the current ledger and leads."""
    lead_rows = conn.execute(
        "SELECT lead_id, workflow_id, phone_hash, external_ref, batch_id, phone_last4 FROM leads"
        " ORDER BY lead_id"
    ).fetchall()
    leads = [
        Lead(r["lead_id"], r["workflow_id"], r["external_ref"], r["batch_id"], r["phone_last4"])
        for r in lead_rows
    ]
    lead_ids = {(r["workflow_id"], r["phone_hash"]): r["lead_id"] for r in lead_rows}
    lead_workflow = {lead.lead_id: lead.workflow_id for lead in leads}

    events = [_to_event(r) for r in conn.execute(
        "SELECT event_id, event_type, workflow_id, call_id, occurred_at, phone_hash, payload FROM events"
        " ORDER BY event_id"
    )]
    links, orphans = link_events(events, lead_ids)
    linked = [(e, links[e.event_id]) for e in events if e.event_id in links]
    calls, unknown = build_calls(linked, lead_workflow, catalogue)

    calls_by_lead: dict[int, list[Call]] = defaultdict(list)
    for c in calls:
        calls_by_lead[c.lead_id].append(c)
    summaries = sorted(
        (summarize_lead(lead, calls_by_lead[lead.lead_id], catalogue) for lead in leads),
        # The spec sorts by (workflow_id, external_ref); batch and phone make ties stable.
        key=lambda s: (s.workflow_id, s.external_ref, s.batch_id, s.phone_last4),
    )
    return Derivation(
        leads=summaries,
        daily=daily_overview(calls, catalogue),
        orphan_events=orphans,
        unknown_disposition_events=unknown,
    )


def _to_event(row: sqlite3.Row) -> Event:
    code = None
    if row["event_type"] in DISPOSITION_TYPES:
        payload = json.loads(row["payload"])
        code = payload.get("disposition_code") if isinstance(payload, dict) else None
    return Event(
        event_id=row["event_id"],
        event_type=row["event_type"],
        workflow_id=row["workflow_id"],
        call_id=row["call_id"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]) if row["occurred_at"] else None,
        phone_hash=row["phone_hash"],
        code=code,
    )
