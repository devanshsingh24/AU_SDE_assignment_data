"""Workflow lead schemas and the disposition catalogue (``assignment-a-data/schemas/``)."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from ledger.normalize import normalize_header


@dataclass(frozen=True)
class Field:
    name: str
    type: str  # string | phone | date | decimal | enum
    required: bool = False
    pii: bool = False
    reference: bool = False
    values: tuple[str, ...] = ()
    default: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowSchema:
    workflow_id: str
    fields: tuple[Field, ...]

    @property
    def phone_field(self) -> Field:
        return next(f for f in self.fields if f.type == "phone")

    @property
    def reference_field(self) -> Field:
        return next(f for f in self.fields if f.reference)

    @property
    def pii_fields(self) -> tuple[Field, ...]:
        """Fields stored only encrypted (``pii: true``), the phone included."""
        return tuple(f for f in self.fields if f.pii)

    @property
    def attribute_fields(self) -> tuple[Field, ...]:
        """Everything that is not the phone, PII, or the reference."""
        return tuple(
            f for f in self.fields if not f.pii and not f.reference and f.type != "phone"
        )

    def map_headers(self, headers: list[str]) -> dict[int, str]:
        """Map column index → field name. Spec: Lead files › Header matching.

        Unknown columns are left out. If two columns map to the same field,
        the first one wins.
        """
        lookup: dict[str, str] = {}
        for f in self.fields:
            for label in (f.name, *f.aliases):
                lookup.setdefault(normalize_header(label), f.name)

        mapping: dict[int, str] = {}
        taken: set[str] = set()
        for index, header in enumerate(headers):
            name = lookup.get(normalize_header(header))
            if name is not None and name not in taken:
                mapping[index] = name
                taken.add(name)
        return mapping


def load_schemas(schemas_dir: Path) -> dict[str, WorkflowSchema]:
    """Load every ``*.json`` workflow schema in ``schemas_dir``, keyed by workflow_id."""
    schemas = {}
    for path in sorted(schemas_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        fields = tuple(
            Field(
                name=f["name"],
                type=f["type"],
                required=f.get("required", False),
                pii=f.get("pii", False),
                reference=f.get("reference", False),
                values=tuple(f.get("values", ())),
                default=f.get("default"),
                aliases=tuple(f.get("aliases", ())),
            )
            for f in raw["fields"]
        )
        schemas[raw["workflow_id"]] = WorkflowSchema(raw["workflow_id"], fields)
    return schemas


@dataclass(frozen=True)
class Disposition:
    is_terminal: bool
    is_success: bool


# (workflow_id, disposition code) → flags
Catalogue = dict[tuple[str, str], Disposition]


def load_dispositions(path: Path) -> Catalogue:
    """Load ``dispositions.csv``. Codes are per workflow: the same code can differ."""
    with path.open(encoding="utf-8", newline="") as f:
        return {
            (row["workflow_id"], row["code"]): Disposition(
                is_terminal=row["is_terminal"].strip().lower() == "true",
                is_success=row["is_success"].strip().lower() == "true",
            )
            for row in csv.DictReader(f)
        }
