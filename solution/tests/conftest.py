"""Shared fixtures: a fresh database per test and a PII helper with test keys."""

import pytest
from cryptography.fernet import Fernet

from ledger.db import connect, init_schema
from ledger.pii import Pii


@pytest.fixture
def conn(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def pii():
    return Pii(b"test-hmac-key", Fernet.generate_key())
