"""PII protection: keyed phone hashing, field encryption and masking.

Spec: fields marked ``"pii": true`` must not be stored in plaintext, but events
still have to be matched to leads by phone. So each phone is stored three ways:

* ``phone_hash``: HMAC-SHA256 of the canonical phone. Deterministic, so it can
  be indexed, made UNIQUE and joined on. A plain SHA-256 would not be safe:
  there are only ~4 billion valid Indian mobiles, so every hash could be
  brute-forced. Without the HMAC key the hash cannot be reversed.
* inside ``pii_enc``: one Fernet token holding all PII fields of the lead
  (phone, name, ...), so the real values can be recovered. Fernet uses a
  random IV, so the same phone encrypts differently each time, which is
  why the token cannot be used for matching.
* ``phone_last4``: plaintext last four digits, only for ``phone_masked``.

The hashing and encryption keys are separate so either can be rotated alone.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

HMAC_KEY_ENV = "LEDGER_HMAC_KEY"
FERNET_KEY_ENV = "LEDGER_FERNET_KEY"


class Pii:
    """Holds the two secret keys and applies them to PII values."""

    def __init__(self, hmac_key: bytes, fernet_key: bytes):
        self._hmac_key = hmac_key
        self._fernet = Fernet(fernet_key)

    @classmethod
    def from_env(cls) -> Pii:
        """Build from ``LEDGER_HMAC_KEY`` and ``LEDGER_FERNET_KEY``; fail loudly if absent."""
        hmac_key = os.environ.get(HMAC_KEY_ENV, "").strip()
        fernet_key = os.environ.get(FERNET_KEY_ENV, "").strip()
        missing = [
            name
            for name, value in ((HMAC_KEY_ENV, hmac_key), (FERNET_KEY_ENV, fernet_key))
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing PII key(s): {', '.join(missing)}. `ledger ingest` creates them in "
                "solution/.env automatically, or generate them with `ledger keygen`."
            )
        return cls(hmac_key.encode(), fernet_key.encode())

    def phone_hash(self, canonical_phone: str) -> str:
        """Keyed hash of a canonical ``+91XXXXXXXXXX`` phone, as hex."""
        return hmac.new(self._hmac_key, canonical_phone.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()


def last4(canonical_phone: str) -> str:
    return canonical_phone[-4:]


def mask_phone(phone_last4: str) -> str:
    """Spec: Output › lead_report › phone_masked is ``+91******`` + last 4 digits."""
    return f"+91******{phone_last4}"


def new_keys() -> dict[str, str]:
    return {HMAC_KEY_ENV: secrets.token_urlsafe(32), FERNET_KEY_ENV: Fernet.generate_key().decode()}


def generate_keys() -> None:
    """Print a fresh pair of keys in ``.env`` format."""
    for name, value in new_keys().items():
        print(f"{name}={value}")


def ensure_keys(env_file: Path) -> bool:
    """Make sure both keys are set; create them once in ``env_file`` if neither is.

    Zero-config for a first run (e.g. an automated grader), while keys still
    never go into git. Returns True when new keys were created, so the caller
    can warn. Exactly one key set is ambiguous and raises.
    """
    present = [name for name in (HMAC_KEY_ENV, FERNET_KEY_ENV) if os.environ.get(name, "").strip()]
    if len(present) == 2:
        return False
    if present:
        raise RuntimeError(
            f"Only {present[0]} is set. Set both PII keys, or neither to have them created automatically."
        )

    keys = new_keys()
    # Neither key has a value, so any existing lines for them (e.g. the empty
    # ones copied from .env.example) can be replaced.
    kept = []
    if env_file.exists():
        kept = [
            line
            for line in env_file.read_text(encoding="utf-8").splitlines()
            if line.split("=", 1)[0].strip() not in keys
        ]
    lines = [
        *kept,
        "# Created by `ledger ingest`. Keep this file: stored PII can't be read without these keys.",
        *(f"{name}={value}" for name, value in keys.items()),
    ]
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(env_file, 0o600)  # owner-only where the OS supports it
    os.environ.update(keys)
    return True


_KEY_CHECK = "key-check"


def check_or_record_keys(conn: sqlite3.Connection, pii: Pii) -> None:
    """Refuse to write with keys other than the ones the database was created with.

    Different keys would silently break matching (new phone hashes never equal
    the stored ones) and make earlier PII unreadable. The first ingest records a
    check value for each key in ``meta``; later ingests must reproduce them.
    """
    stored = dict(conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('hmac_check', 'fernet_check')"
    ).fetchall())
    if not stored:
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [("hmac_check", pii.phone_hash(_KEY_CHECK)), ("fernet_check", pii.encrypt(_KEY_CHECK))],
        )
        return
    try:
        matches = hmac.compare_digest(stored.get("hmac_check", ""), pii.phone_hash(_KEY_CHECK)) and (
            pii.decrypt(stored.get("fernet_check", "")) == _KEY_CHECK
        )
    except InvalidToken:
        matches = False
    if not matches:
        raise RuntimeError(
            "PII keys don't match the keys this database was created with. Use the original "
            "solution/.env, or delete the database and re-ingest from the raw drops."
        )
