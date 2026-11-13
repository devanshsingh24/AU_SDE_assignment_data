# Interaction Ledger & Reporting Pipeline (Assignment A)

Ingests lead CSVs and call-event JSONL drops into a SQLite database with an append-only event ledger, then writes the five report files from the output contract.

## Setup (from a clean clone, repo root, Python 3.11+)

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e solution          # installs the `ledger` command
```

**PII keys:** no setup needed. If neither key is set, the first `ledger ingest` creates both in `solution/.env` (git-ignored) and prints a warning. Keep that file: stored PII can't be read without it.
- To use your own keys, put `LEDGER_HMAC_KEY` / `LEDGER_FERNET_KEY` in `solution/.env` (see `.env.example`; `ledger keygen` prints a fresh pair) or in the environment, which takes precedence.
- Setting only one of the two keys is an error.
- A database remembers which keys created it and refuses others, so a lost or changed `.env` can't silently break phone matching.

## Run

```bash
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

ledger ingest data/drop_1
ledger ingest data/drop_2
ledger report --out reports/
```

- `ledger ingest <drop> [<drop> ...]` ingests drops in the order given; `ledger ingest data/drop_1 data/drop_2` is the "all at once" form.
- Re-ingesting a drop is safe: files already ingested (same content hash) are skipped.
- A file re-sent under the same name with **different** content is refused, and so is a lead CSV that isn't valid UTF-8. `ingest` stops there with exit code 1; files before it stay ingested, and a re-run continues after the file is fixed.
- `--db PATH` (before the subcommand) picks the database file. The default is `$LEDGER_DB_PATH`, or `ledger.db` in the current directory.
- `report` needs no keys: it never decrypts anything.
- Without installing, `cd solution && python -m ledger ...` works too.

## Test

```bash
cd solution
pytest
ruff check . && ruff format --check .
```

CI (`.github/workflows/tests.yml`) runs lint, the tests and a real generate → ingest → report run on Linux with Python 3.11 and 3.12.

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
