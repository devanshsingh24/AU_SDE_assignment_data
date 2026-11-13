# Interaction Ledger & Reporting Pipeline (Assignment A)

Ingests lead CSVs and call-event JSONL drops into a SQLite database with an append-only event ledger, then writes the five report files from the output contract.

## Setup (from a clean clone, repo root)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install -e solution          # installs the `ledger` command

cp solution/.env.example solution/.env   # Windows: copy solution\.env.example solution\.env
ledger keygen                            # paste the two printed lines into solution/.env
```

`.env` is found automatically, and variables already set in the environment take precedence. If either key is missing, `ingest` stops with a clear error.

## Run

```bash
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

ledger ingest data/drop_1
ledger ingest data/drop_2
ledger report --out reports/
```

- `ledger ingest <drop> [<drop> ...]` ingests drops in the order given; `ledger ingest data/drop_1 data/drop_2` is the "all at once" form.
- Re-ingesting a drop is safe: files already ingested (same content hash) are skipped.
- `--db PATH` (before the subcommand) picks the database file. The default is `$LEDGER_DB_PATH`, or `ledger.db` in the current directory.
- `report` needs no keys: it never decrypts anything.
- Without installing, `cd solution && python -m ledger ...` works too.

## Test

```bash
cd solution
pytest
```

The end-to-end tests generate seed-7 data, run the real CLI, and check that:
- drop-by-drop ingest, a re-ingest, and a single "all at once" run produce **byte-identical** reports
- the reports are internally consistent

## Layout

| Module | Responsibility |
|---|---|
| `ledger/normalize.py` | Pure field parsers: header, phone, date, decimal, enum, timestamp |
| `ledger/schema.py` | Workflow schemas, header mapping, disposition catalogue |
| `ledger/pii.py` | HMAC phone hash, Fernet encryption, masking, key loading |
| `ledger/db.py` | SQLite schema, connection settings, `transaction()` |
| `ledger/ingest_leads.py` | Lead validation, rejection, merging |
| `ledger/ingest_events.py` | Event ledger: malformed and duplicate handling |
| `ledger/derive.py` | Linking, orphans, calls, effective disposition, buckets, daily stats |
| `ledger/report.py` | The five output files |
| `ledger/cli.py` | `ingest` / `report` / `keygen` |

Design and decisions: see [`../SUBMISSION.md`](../SUBMISSION.md).
