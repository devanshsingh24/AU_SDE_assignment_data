"""Lead ingestion. Spec: Assignment A › Rules › Lead files.

For each ``<drop>/leads/<workflow_id>/*.csv`` (workflow, then filename order):

1. Skip the file if its sha256 is already in ``ingested_files`` (idempotency);
   refuse a file of the same name whose content changed (see ``already_ingested``).
2. Map headers to schema fields; ignore unknown columns.
3. Skip blank rows; number the rest from 1.
4. Validate fields in schema order; the first failure rejects the row.
5. A valid row creates the lead for ``(workflow_id, phone)`` or, if it
   already exists, merges into it (non-null values overwrite, batch_id kept).
6. Record every row's outcome in ``lead_rows`` (no PII there).

The whole file runs in one transaction, so it is either fully ingested or not
at all.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ledger.db import IngestError, already_ingested, transaction
from ledger.normalize import normalize_phone, parse_date, parse_decimal, parse_enum
from ledger.pii import Pii, last4
from ledger.schema import Field, WorkflowSchema

INVALID_PHONE = "invalid_phone"


@dataclass
class ValidatedRow:
    values: dict[str, str | None] = field(default_factory=dict)
    reason: str | None = None  # set when the row is rejected
    warnings: int = 0


@dataclass
class FileResult:
    path: str
    skipped: bool = False
    created: int = 0
    merged: int = 0
    rejected: int = 0


def _parse_field(f: Field, value: str) -> str | None:
    """Canonical string form of a non-empty value, or None if it is invalid."""
    if f.type == "phone":
        return normalize_phone(value)
    if f.type == "date":
        parsed = parse_date(value)
        return parsed.isoformat() if parsed else None
    if f.type == "decimal":
        amount = parse_decimal(value)
        return str(amount) if amount is not None else None
    if f.type == "enum":
        return parse_enum(value, list(f.values))
    return value  # string


def validate_row(schema: WorkflowSchema, raw: dict[str, str]) -> ValidatedRow:
    """Validate one row (field name → raw cell). Pure: no database, no files."""
    result = ValidatedRow()
    for f in schema.fields:
        value = raw.get(f.name, "").strip()
        if not value:
            result.values[f.name] = None
            if f.required and result.reason is None:
                result.reason = f"missing_required:{f.name}"
            continue

        parsed = _parse_field(f, value)
        result.values[f.name] = parsed
        if parsed is not None:
            continue
        if f.type == "phone":
            # Spec: a present-but-invalid phone rejects the row, required or not.
            result.reason = result.reason or INVALID_PHONE
        elif f.required:
            # Unreachable with the given schemas (required fields are string or
            # phone), but a future schema could have a required date.
            result.reason = result.reason or f"invalid:{f.name}"
        else:
            result.warnings += 1

    if result.reason is not None:
        result.warnings = 0  # spec: warnings count only for rows that aren't rejected
    return result


def ingest_leads(
    conn: sqlite3.Connection,
    pii: Pii,
    schemas: dict[str, WorkflowSchema],
    drop_dir: Path,
    on_file: Callable[[FileResult], None] | None = None,
) -> list[FileResult]:
    """Ingest every lead CSV of one drop, in (workflow, filename) order.

    ``on_file`` is called as soon as each file is done, so progress is visible
    even if a later file fails.
    """
    results = []
    leads_dir = drop_dir / "leads"
    if not leads_dir.is_dir():
        return results
    for workflow_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
        schema = schemas.get(workflow_dir.name)
        if schema is None:
            print(f"warning: no schema for workflow '{workflow_dir.name}', skipping {workflow_dir}")
            continue
        for path in sorted(workflow_dir.glob("*.csv")):
            results.append(ingest_lead_file(conn, pii, schema, path))
            if on_file:
                on_file(results[-1])
    return results


def ingest_lead_file(
    conn: sqlite3.Connection, pii: Pii, schema: WorkflowSchema, path: Path
) -> FileResult:
    """Ingest one CSV. Rows are merged in memory, then written with executemany."""
    result = FileResult(path=str(path))
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if already_ingested(conn, path, sha256, "leads"):
        result.skipped = True
        return result

    batch_id = path.stem
    with transaction(conn):
        file_id = conn.execute(
            "INSERT INTO ingested_files (sha256, path, kind, workflow_id, ingested_at)"
            " VALUES (?, ?, 'leads', ?, ?)",
            (sha256, str(path), schema.workflow_id, datetime.now(timezone.utc).isoformat()),
        ).lastrowid

        existing_ids = dict(conn.execute(
            "SELECT phone_hash, lead_id FROM leads WHERE workflow_id = ?", (schema.workflow_id,)
        ).fetchall())
        touched: dict[str, _Lead] = {}  # phone_hash → lead state, in first-seen order
        lead_rows = []

        reader = _csv_rows(path)
        headers = next(reader, [])
        columns = schema.map_headers(headers)
        row_number = 0
        for cells in reader:
            if all(not cell.strip() for cell in cells):
                continue  # blank rows are skipped and not counted
            row_number += 1
            raw = {name: cells[i] if i < len(cells) else "" for i, name in columns.items()}
            row = validate_row(schema, raw)

            if row.reason is not None:
                outcome = "rejected"
                result.rejected += 1
            else:
                phone_hash = pii.phone_hash(row.values[schema.phone_field.name])
                lead = touched.get(phone_hash)
                if lead is None and phone_hash in existing_ids:
                    lead = touched[phone_hash] = _Lead.load(conn, pii, existing_ids[phone_hash])
                if lead is None:
                    touched[phone_hash] = _Lead.new(schema, phone_hash, batch_id, row.values)
                    outcome = "created"
                    result.created += 1
                else:
                    lead.merge(schema, row.values)
                    outcome = "merged"
                    result.merged += 1
            lead_rows.append(
                (file_id, row_number, schema.workflow_id, batch_id, outcome, row.reason, row.warnings)
            )

        # Each lead is encrypted once, after all of this file's merges.
        conn.executemany(
            "INSERT INTO leads (workflow_id, phone_hash, phone_last4, pii_enc, external_ref,"
            " batch_id, attributes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (schema.workflow_id, lead.phone_hash, lead.phone_last4, pii.encrypt(json.dumps(lead.pii)),
                 lead.external_ref, lead.batch_id, json.dumps(lead.attributes))
                for lead in touched.values() if lead.lead_id is None
            ],
        )
        conn.executemany(
            "UPDATE leads SET pii_enc = ?, external_ref = ?, attributes = ? WHERE lead_id = ?",
            [
                (pii.encrypt(json.dumps(lead.pii)), lead.external_ref, json.dumps(lead.attributes),
                 lead.lead_id)
                for lead in touched.values() if lead.lead_id is not None
            ],
        )
        conn.executemany(
            "INSERT INTO lead_rows (file_id, row_number, workflow_id, batch_id,"
            " outcome, reason, field_warnings) VALUES (?, ?, ?, ?, ?, ?, ?)",
            lead_rows,
        )
    return result


def _csv_rows(path: Path):
    """Yield CSV rows; an undecodable or broken file becomes an IngestError.

    Raised inside the file's transaction, so nothing from the file is kept.
    """
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            yield from csv.reader(f)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise IngestError(f"{path}: not a readable UTF-8 CSV ({exc})") from exc


@dataclass
class _Lead:
    """In-memory state of one lead while a file is being ingested."""

    lead_id: int | None  # None until it is inserted
    phone_hash: str
    phone_last4: str
    batch_id: str
    external_ref: str
    pii: dict[str, str | None]  # every pii:true field, phone included
    attributes: dict[str, str | None]

    @classmethod
    def new(cls, schema: WorkflowSchema, phone_hash: str, batch_id: str, values: dict) -> _Lead:
        return cls(
            lead_id=None,
            phone_hash=phone_hash,
            phone_last4=last4(values[schema.phone_field.name]),
            batch_id=batch_id,
            external_ref=values[schema.reference_field.name],
            pii={f.name: values[f.name] for f in schema.pii_fields},
            attributes={f.name: values[f.name] for f in schema.attribute_fields},
        )

    @classmethod
    def load(cls, conn: sqlite3.Connection, pii: Pii, lead_id: int) -> _Lead:
        row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        return cls(
            lead_id=lead_id,
            phone_hash=row["phone_hash"],
            phone_last4=row["phone_last4"],
            batch_id=row["batch_id"],
            external_ref=row["external_ref"],
            pii=json.loads(pii.decrypt(row["pii_enc"])),
            attributes=json.loads(row["attributes"]),
        )

    def merge(self, schema: WorkflowSchema, values: dict) -> None:
        """Spec: a later valid row overwrites only fields where its value is non-null.

        batch_id and the phone never change: they define the lead.
        """
        self.pii = _merge(self.pii, {f.name: values[f.name] for f in schema.pii_fields})
        self.attributes = _merge(self.attributes, {f.name: values[f.name] for f in schema.attribute_fields})
        self.external_ref = values[schema.reference_field.name] or self.external_ref


def _merge(old: dict, new: dict) -> dict:
    return {**old, **{k: v for k, v in new.items() if v is not None}}
