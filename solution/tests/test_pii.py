"""Tests for ledger.pii."""
import pytest
from cryptography.fernet import Fernet

from ledger.pii import FERNET_KEY_ENV, HMAC_KEY_ENV, Pii, last4, mask_phone

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
