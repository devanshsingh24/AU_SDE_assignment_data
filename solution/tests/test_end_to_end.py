"""End to end through the CLI on generator data: idempotency and order independence.

Uses a seed other than 42 on purpose: the grader runs on an unseen seed.
"""
import csv
import json
import re
import subprocess
import sys
from collections import Counter

import pytest
from cryptography.fernet import Fernet

from ledger import config
from ledger.cli import main
from ledger.config import REPO_ROOT
from ledger.pii import FERNET_KEY_ENV, HMAC_KEY_ENV

GENERATOR = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"
REPORT_FILES = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    subprocess.run(
        [sys.executable, str(GENERATOR), "--seed", "7", "--scale", "small", "--out", str(out)],
        check=True, capture_output=True,
    )
    return out


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    # Don't let a developer's solution/.env leak into these tests: the CLI
    # loads it on start, which would silently refill a key a test removed.
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setenv(HMAC_KEY_ENV, "e2e-hmac-key")
    monkeypatch.setenv(FERNET_KEY_ENV, Fernet.generate_key().decode())


def snapshot(out_dir):
    return {name: (out_dir / name).read_bytes() for name in REPORT_FILES}


def rows(path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_incremental_rerun_and_single_run_give_identical_reports(data, tmp_path):
    db = str(tmp_path / "incremental.db")
    assert main(["--db", db, "ingest", str(data / "drop_1")]) == 0
    assert main(["--db", db, "ingest", str(data / "drop_2")]) == 0
    assert main(["--db", db, "report", "--out", str(tmp_path / "r1")]) == 0

    # Re-ingesting a drop already ingested must change nothing.
    assert main(["--db", db, "ingest", str(data / "drop_1")]) == 0
    assert main(["--db", db, "report", "--out", str(tmp_path / "r2")]) == 0

    # Everything at once, in a fresh database.
    single = str(tmp_path / "single.db")
    assert main(["--db", single, "ingest", str(data / "drop_1"), str(data / "drop_2")]) == 0
    assert main(["--db", single, "report", "--out", str(tmp_path / "r3")]) == 0

    first = snapshot(tmp_path / "r1")
    assert snapshot(tmp_path / "r2") == first
    assert snapshot(tmp_path / "r3") == first


@pytest.fixture(scope="module")
def reports(data, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("run")
    db = str(tmp / "ledger.db")
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "load_env", lambda: None)
    mp.setenv(HMAC_KEY_ENV, "e2e-hmac-key")
    mp.setenv(FERNET_KEY_ENV, Fernet.generate_key().decode())
    main(["--db", db, "ingest", str(data / "drop_1"), str(data / "drop_2")])
    main(["--db", db, "report", "--out", str(tmp / "out")])
    mp.undo()
    return tmp / "out"


def test_reports_are_internally_consistent(reports):
    dq = json.loads((reports / "data_quality.json").read_text(encoding="utf-8"))
    leads = rows(reports / "lead_report.csv")
    batches = rows(reports / "batch_summary.csv")
    rejections = rows(reports / "rejections.csv")
    daily = rows(reports / "daily_overview.csv")

    # Every lead row read is either a new lead, a merged duplicate, or rejected.
    assert dq["lead_rows_total"] == len(leads) + dq["duplicate_lead_rows_merged"] + dq["lead_rows_rejected"]
    assert dq["lead_rows_rejected"] == len(rejections) == sum(dq["rejections_by_reason"].values())

    # Buckets add up, per batch and overall.
    buckets = ["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]
    for b in batches:
        assert int(b["total_leads"]) == sum(int(b[k]) for k in buckets)
    assert sum(int(b["total_leads"]) for b in batches) == len(leads)
    assert Counter(r["bucket"] for r in leads) == Counter(
        {k: sum(int(b[k]) for b in batches) for k in buckets if sum(int(b[k]) for b in batches)}
    )

    # Every counted call appears on exactly one day.
    assert sum(int(d["total_calls"]) for d in daily) == sum(int(r["total_calls"]) for r in leads)
    assert sum(int(d["connected_calls"]) for d in daily) == sum(int(r["connected_calls"]) for r in leads)


def test_report_sorting_and_formats(reports):
    leads = rows(reports / "lead_report.csv")
    assert leads == sorted(leads, key=lambda r: (r["workflow_id"], r["external_ref"]))
    rejections = rows(reports / "rejections.csv")
    assert rejections == sorted(
        rejections, key=lambda r: (r["workflow_id"], r["batch_id"], int(r["row_number"]))
    )
    for r in leads:
        assert r["phone_masked"].startswith("+91******") and len(r["phone_masked"]) == 13
        assert r["ever_success"] in ("true", "false")
        assert r["last_call_at"] == "" or r["last_call_at"].endswith("+05:30")
    for d in rows(reports / "daily_overview.csv"):
        whole, frac = d["connectivity_rate"].split(".")
        assert len(frac) == 4


def test_no_full_phone_number_in_any_report(reports):
    text = "".join((reports / name).read_text(encoding="utf-8") for name in REPORT_FILES)
    assert not re.search(r"\d{10}", text)


def test_cli_fails_cleanly_without_keys(monkeypatch, tmp_path, data, capsys):
    monkeypatch.delenv(HMAC_KEY_ENV)
    code = main(["--db", str(tmp_path / "x.db"), "ingest", str(data / "drop_1")])
    assert code == 1
    assert HMAC_KEY_ENV in capsys.readouterr().err


def test_cli_rejects_missing_directory(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "x.db"), "ingest", str(tmp_path / "nope")]) == 2
    assert "not a directory" in capsys.readouterr().err
