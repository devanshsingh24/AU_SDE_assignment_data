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

import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet

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
                f"Missing PII key(s): {', '.join(missing)}. Generate them with "
                "`ledger keygen` and put them in solution/.env (see .env.example)."
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


def generate_keys() -> None:
    """Print a fresh pair of keys in ``.env`` format."""
    print(f"{HMAC_KEY_ENV}={secrets.token_urlsafe(32)}")
    print(f"{FERNET_KEY_ENV}={Fernet.generate_key().decode()}")
