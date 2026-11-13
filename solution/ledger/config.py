"""Paths and environment settings shared by the pipeline."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "assignment-a-data" / "schemas"
DISPOSITIONS_CSV = SCHEMAS_DIR / "dispositions.csv"
DEFAULT_DB_PATH = "ledger.db"
# Git-ignored (root .gitignore has `.env`). Holds the PII keys.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_env() -> None:
    """Load ``solution/.env`` into ``os.environ`` (existing variables win).

    Called by the CLI only, so tests stay independent of any developer's ``.env``.
    """
    load_dotenv(ENV_FILE, override=False)


def db_path() -> str:
    """Database file location: ``LEDGER_DB_PATH`` or ``ledger.db`` in the cwd."""
    return os.environ.get("LEDGER_DB_PATH", DEFAULT_DB_PATH)
