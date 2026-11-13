"""Command line: ``ledger ingest <drop>...``, ``ledger report --out <dir>``, ``ledger keygen``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ledger import config
from ledger.db import connect, init_schema
from ledger.ingest_events import ingest_events
from ledger.ingest_leads import ingest_leads
from ledger.pii import Pii, generate_keys
from ledger.report import write_reports
from ledger.schema import load_dispositions, load_schemas


def main(argv: list[str] | None = None) -> int:
    config.load_env()
    args = _parser().parse_args(argv)
    if args.command == "keygen":
        generate_keys()
        return 0

    db = args.db or config.db_path()
    conn = connect(db)
    try:
        init_schema(conn)
        if args.command == "ingest":
            return _ingest(conn, args.drops)
        write_reports(conn, load_dispositions(config.DISPOSITIONS_CSV), args.out)
        print(f"reports written to {args.out}")
        return 0
    finally:
        conn.close()


def _ingest(conn, drops: list[Path]) -> int:
    missing = [d for d in drops if not d.is_dir()]
    if missing:
        print(f"error: not a directory: {', '.join(map(str, missing))}", file=sys.stderr)
        return 2
    try:
        pii = Pii.from_env()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    schemas = load_schemas(config.SCHEMAS_DIR)
    # Drops are processed in the order given: it decides which duplicate lead row comes first.
    for drop in drops:
        for r in ingest_leads(conn, pii, schemas, drop):
            status = "skipped (already ingested)" if r.skipped else (
                f"created={r.created} merged={r.merged} rejected={r.rejected}")
            print(f"{r.path}: {status}")
        for r in ingest_events(conn, pii, drop):
            status = "skipped (already ingested)" if r.skipped else (
                f"lines={r.lines} stored={r.stored} malformed={r.malformed} duplicates={r.duplicates}")
            print(f"{r.path}: {status}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger", description="Interaction ledger & reporting pipeline")
    parser.add_argument("--db", help="SQLite file (default: $LEDGER_DB_PATH or ledger.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="ingest one or more drop directories, in order")
    ingest.add_argument("drops", nargs="+", type=Path)

    report = sub.add_parser("report", help="write the five report files")
    report.add_argument("--out", required=True, type=Path)

    sub.add_parser("keygen", help="print a fresh pair of PII keys for .env")
    return parser


if __name__ == "__main__":
    sys.exit(main())
