"""Tests for ledger.pii."""
import pytest
from cryptography.fernet import Fernet

from ledger.pii import (
    FERNET_KEY_ENV,
    HMAC_KEY_ENV,
    Pii,
    check_or_record_keys,
    ensure_keys,
    last4,
    mask_phone,
)

PHONE = "+919876543210"


def test_phone_hash_is_deterministic(pii):
    assert pii.phone_hash(PHONE) == pii.phone_hash(PHONE)


def test_phone_hash_depends_on_key():
    a = Pii(b"key-a", Fernet.generate_key())
    b = Pii(b"key-b", Fernet.generate_key())
    assert a.phone_hash(PHONE) != b.phone_hash(PHONE)


def test_phone_hash_does_not_contain_the_phone(pii):
    assert "9876543210" not in pii.phone_hash(PHONE)


def test_encrypt_round_trip(pii):
    token = pii.encrypt("Ananya Iyer")
    assert "Ananya" not in token
    assert pii.decrypt(token) == "Ananya Iyer"


def test_encrypting_twice_gives_different_tokens(pii):
    # Why encrypted values can't be used for matching: Fernet uses a random IV.
    assert pii.encrypt(PHONE) != pii.encrypt(PHONE)


def test_mask_phone():
    assert mask_phone(last4(PHONE)) == "+91******3210"


def test_from_env_reads_keys(monkeypatch):
    monkeypatch.setenv(HMAC_KEY_ENV, "env-hmac-key")
    monkeypatch.setenv(FERNET_KEY_ENV, Fernet.generate_key().decode())
    pii = Pii.from_env()
    assert pii.decrypt(pii.encrypt("x")) == "x"


@pytest.mark.parametrize("missing", [HMAC_KEY_ENV, FERNET_KEY_ENV])
def test_from_env_fails_loudly_when_a_key_is_missing(monkeypatch, missing):
    monkeypatch.setenv(HMAC_KEY_ENV, "env-hmac-key")
    monkeypatch.setenv(FERNET_KEY_ENV, Fernet.generate_key().decode())
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        Pii.from_env()


# ---------- ensure_keys / check_or_record_keys ----------

@pytest.fixture
def no_keys(monkeypatch):
    monkeypatch.delenv(HMAC_KEY_ENV, raising=False)
    monkeypatch.delenv(FERNET_KEY_ENV, raising=False)


def test_ensure_keys_does_nothing_when_both_are_set(monkeypatch, tmp_path):
    monkeypatch.setenv(HMAC_KEY_ENV, "a")
    monkeypatch.setenv(FERNET_KEY_ENV, Fernet.generate_key().decode())
    assert ensure_keys(tmp_path / ".env") is False
    assert not (tmp_path / ".env").exists()


def test_ensure_keys_creates_both_and_replaces_empty_template_lines(no_keys, tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"{HMAC_KEY_ENV}=\n{FERNET_KEY_ENV}=\nLEDGER_DB_PATH=ledger.db\n", encoding="utf-8")
    assert ensure_keys(env) is True

    lines = env.read_text(encoding="utf-8").splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    assert values["LEDGER_DB_PATH"] == "ledger.db"
    assert values[HMAC_KEY_ENV] and values[FERNET_KEY_ENV]
    assert sum(line.startswith(HMAC_KEY_ENV) for line in lines) == 1  # empty line replaced
    Pii.from_env()  # the new keys are usable straight away


def test_ensure_keys_refuses_a_single_key(no_keys, monkeypatch, tmp_path):
    monkeypatch.setenv(HMAC_KEY_ENV, "only-one")
    with pytest.raises(RuntimeError, match="Only"):
        ensure_keys(tmp_path / ".env")


def test_key_check_is_recorded_then_enforced(conn, pii):
    check_or_record_keys(conn, pii)
    check_or_record_keys(conn, pii)  # same keys: fine
    with pytest.raises(RuntimeError, match="don't match"):
        check_or_record_keys(conn, Pii(b"other-hmac-key", Fernet.generate_key()))
