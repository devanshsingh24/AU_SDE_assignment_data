"""The five report files. Spec: Assignment A › Output contract.

Reports are rebuilt from scratch on every run, from the database only (see
``derive.py``). They never need the PII keys: masking uses the stored last
four digits, so ``report`` can run without decrypting anything.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

from ledger.derive import Bucket, Derivation, derive
from ledger.pii import mask_phone
from ledger.schema import Catalogue

BATCH_SUMMARY_HEADER = [
    "workflow_id",
    "batch_id",
    "total_leads",
    Bucket.YET_TO_REACH_OUT,
    Bucket.TELEPHONY_FAILED,
    Bucket.DIALED,
    Bucket.COMPLETED,
]
DAILY_OVERVIEW_HEADER = [
    "workflow_id",
    "date",
    "total_calls",
    "connected_calls",
    "connectivity_rate",
    "success_leads",
]
LEAD_REPORT_HEADER = [
    "workflow_id",
    "external_ref",
    "batch_id",
    "phone_masked",
    "bucket",
    "total_calls",
    "connected_calls",
    "last_call_at",
    "current_disposition",
    "ever_success",
]
REJECTIONS_HEADER = ["workflow_id", "batch_id", "row_number", "reason"]


def write_reports(conn: sqlite3.Connection, catalogue: Catalogue, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    d = derive(conn, catalogue)
    _write_csv(out_dir / "batch_summary.csv", BATCH_SUMMARY_HEADER, batch_summary_rows(conn, d))
    _write_csv(out_dir / "daily_overview.csv", DAILY_OVERVIEW_HEADER, daily_overview_rows(d))
    _write_csv(out_dir / "lead_report.csv", LEAD_REPORT_HEADER, lead_report_rows(d))
    _write_csv(out_dir / "rejections.csv", REJECTIONS_HEADER, rejection_rows(conn))
    with (out_dir / "data_quality.json").open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data_quality(conn, d), f, indent=2)
        f.write("\n")


def batch_summary_rows(conn: sqlite3.Connection, d: Derivation) -> list[list]:
    """One row per uploaded batch. Batches whose rows created no lead show zeros."""
    counts: dict[tuple[str, str], Counter] = {
        (r["workflow_id"], r["batch_id"]): Counter()
        for r in conn.execute("SELECT DISTINCT workflow_id, batch_id FROM lead_rows")
    }
    for lead in d.leads:
        counts.setdefault((lead.workflow_id, lead.batch_id), Counter())[lead.bucket] += 1
    return [
        [
            workflow_id,
            batch_id,
            sum(c.values()),
            c[Bucket.YET_TO_REACH_OUT],
            c[Bucket.TELEPHONY_FAILED],
            c[Bucket.DIALED],
            c[Bucket.COMPLETED],
        ]
        for (workflow_id, batch_id), c in sorted(counts.items())
    ]


def daily_overview_rows(d: Derivation) -> list[list]:
    return [
        [
            r.workflow_id,
            r.date,
            r.total_calls,
            r.connected_calls,
            format_rate(r.connected_calls, r.total_calls),
            r.success_leads,
        ]
        for r in d.daily
    ]


def lead_report_rows(d: Derivation) -> list[list]:
    return [
        [
            s.workflow_id,
            s.external_ref,
            s.batch_id,
            mask_phone(s.phone_last4),
            s.bucket,
            s.total_calls,
            s.connected_calls,
            s.last_call_at,
            s.current_disposition,
            format_bool(s.ever_success),
        ]
        for s in d.leads
    ]


def rejection_rows(conn: sqlite3.Connection) -> list[list]:
    """Rejected rows only. lead_rows holds no PII, so nothing needs masking."""
    return [
        list(r)
        for r in conn.execute(
            "SELECT workflow_id, batch_id, row_number, reason FROM lead_rows"
            " WHERE outcome = 'rejected' ORDER BY workflow_id, batch_id, row_number"
        )
    ]


def data_quality(conn: sqlite3.Connection, d: Derivation) -> dict:
    """Counts in the contract's key order, all derived from the current tables."""
    rows = conn.execute(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(outcome = 'rejected'), 0) AS rejected,"
        " COALESCE(SUM(outcome = 'merged'), 0) AS merged,"
        " COALESCE(SUM(CASE WHEN outcome != 'rejected' THEN field_warnings END), 0) AS warnings"
        " FROM lead_rows"
    ).fetchone()
    by_reason = dict(
        conn.execute(
            "SELECT reason, COUNT(*) FROM lead_rows WHERE outcome = 'rejected'"
            " GROUP BY reason ORDER BY reason"
        ).fetchall()
    )
    lines = conn.execute(
        "SELECT COALESCE(SUM(lines_total), 0), COALESCE(SUM(malformed), 0),"
        " COALESCE(SUM(duplicates), 0) FROM ingested_files WHERE kind = 'events'"
    ).fetchone()
    return {
        "lead_rows_total": rows["total"],
        "lead_rows_rejected": rows["rejected"],
        "rejections_by_reason": by_reason,
        "duplicate_lead_rows_merged": rows["merged"],
        "field_warnings": rows["warnings"],
        "events_total": lines[0],
        "malformed_events": lines[1],
        "duplicate_events_dropped": lines[2],
        "orphan_events": d.orphan_events,
        "unknown_disposition_events": d.unknown_disposition_events,
    }


def format_rate(connected: int, total: int) -> str:
    """connected / total with exactly 4 decimals, e.g. ``0.5625``."""
    return f"{connected / total:.4f}"


def format_bool(value: bool) -> str:
    return "true" if value else "false"


def _write_csv(path: Path, header: list, rows: list[list]) -> None:
    # "\n" line endings so the output is byte-identical on Windows and Linux.
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
