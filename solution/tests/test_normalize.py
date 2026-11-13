"""Tests for ledger.normalize. Cases come from the spec and from generate_data.py."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ledger.normalize import (
    IST,
    normalize_header,
    normalize_phone,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_timestamp,
    to_ist,
)

LANGS = ["en", "hi", "ta", "kn", "te", "mr"]


# ---------- headers ----------

@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param("customer_name", "customer name", id="underscore"),
        pytest.param("  Insured Name ", "insured name", id="trim-and-lower"),
        pytest.param("Mobile   No", "mobile no", id="multiple-spaces"),
        pytest.param("Mobile _\t No", "mobile no", id="mixed-separator-run"),
        pytest.param("PHONE", "phone", id="upper"),
        pytest.param("phone_", "phone", id="trailing-underscore"),
        pytest.param("agent_notes", "agent notes", id="unknown-column-still-normalised"),
    ],
)
def test_normalize_header(raw, expected):
    assert normalize_header(raw) == expected


# ---------- phones ----------

@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("9876543210", id="plain-10"),
        pytest.param("+91 98765 43210", id="plus91-spaced"),
        pytest.param("09876543210", id="leading-zero"),
        pytest.param("91-98765-43210", id="91-dashed"),
        pytest.param("(+91) 9876543210", id="bracketed-plus91"),
        pytest.param("+919876543210", id="plus91-compact"),
    ],
)
def test_normalize_phone_valid_formats(raw):
    assert normalize_phone(raw) == "+919876543210"


@pytest.mark.parametrize("first_digit", ["6", "7", "8", "9"])
def test_normalize_phone_accepts_all_mobile_prefixes(first_digit):
    assert normalize_phone(first_digit + "123456789") == f"+91{first_digit}123456789"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("987654321", id="nine-digits"),
        pytest.param("5876543210", id="bad-prefix-5"),
        pytest.param("022-2345-6789", id="landline"),
        pytest.param("N/A", id="not-a-number"),
        pytest.param("+1 415 555 0134", id="us-number"),
        pytest.param("919876543210123", id="too-long"),
        pytest.param("19876543210", id="11-digits-not-zero"),
        pytest.param("915876543210", id="91-then-bad-prefix"),
        pytest.param("", id="empty"),
    ],
)
def test_normalize_phone_invalid(raw):
    assert normalize_phone(raw) is None


# ---------- dates ----------

@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param("2026-12-01", date(2026, 12, 1), id="iso"),
        pytest.param("01/12/2026", date(2026, 12, 1), id="dd/mm/yyyy-day-first"),
        pytest.param("01-12-2026", date(2026, 12, 1), id="dd-mm-yyyy"),
        pytest.param("01-Dec-2026", date(2026, 12, 1), id="dd-mon-yyyy"),
        pytest.param("01-DEC-2026", date(2026, 12, 1), id="month-upper"),
        pytest.param("01-dec-2026", date(2026, 12, 1), id="month-lower"),
        pytest.param(" 29/02/2028 ", date(2028, 2, 29), id="leap-day-trimmed"),
    ],
)
def test_parse_date_valid(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("31/02/2026", id="impossible-day"),
        pytest.param("29/02/2026", id="not-a-leap-year"),
        pytest.param("2026-13-01", id="month-13"),
        pytest.param("soon", id="word"),
        pytest.param("TBD", id="placeholder"),
        pytest.param("01-Foo-2026", id="bad-month-name"),
        pytest.param("01/12-2026", id="mixed-separators"),
        pytest.param("2026/12/01", id="iso-with-slashes"),
        pytest.param("1/2/2026", id="single-digit-parts"),
        pytest.param("", id="empty"),
    ],
)
def test_parse_date_invalid(raw):
    assert parse_date(raw) is None


# ---------- decimals ----------

@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param("12345", "12345.00", id="integer"),
        pytest.param("12345.67", "12345.67", id="two-places"),
        pytest.param("₹12,345", "12345.00", id="rupee-symbol-commas"),
        pytest.param("₹ 1,23,456", "123456.00", id="indian-grouping"),
        pytest.param("Rs. 500", "500.00", id="rs-dot-space"),
        pytest.param("Rs500", "500.00", id="rs-no-space"),
        pytest.param("rs.500", "500.00", id="rs-dot-lower"),
        pytest.param("INR 1200.5", "1200.50", id="inr"),
        pytest.param("inr1200", "1200.00", id="inr-lower-no-space"),
        pytest.param("10.005", "10.01", id="round-half-up"),
        pytest.param("0", "0.00", id="zero-allowed"),
    ],
)
def test_parse_decimal_valid(raw, expected):
    assert parse_decimal(raw) == Decimal(expected)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("N/A", id="not-applicable"),
        pytest.param("-", id="dash"),
        pytest.param("twelve thousand", id="words"),
        pytest.param("-500", id="negative"),
        pytest.param("₹-500", id="negative-with-prefix"),
        pytest.param("USD 500", id="other-currency"),
        pytest.param("1.2.3", id="two-dots"),
        pytest.param("", id="empty"),
    ],
)
def test_parse_decimal_invalid(raw):
    assert parse_decimal(raw) is None


# ---------- enums ----------

@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param("hi", "hi", id="exact"),
        pytest.param("HI", "hi", id="upper"),
        pytest.param(" ta ", "ta", id="padded"),
    ],
)
def test_parse_enum_valid(raw, expected):
    assert parse_enum(raw, LANGS) == expected


@pytest.mark.parametrize("raw", ["Hindi", "english", "tamil", "e n"])
def test_parse_enum_invalid(raw):
    assert parse_enum(raw, LANGS) is None


# ---------- timestamps ----------

@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("2026-10-05T07:50:21Z", id="utc-z"),
        pytest.param("2026-10-05T13:20:21+05:30", id="ist-offset"),
        pytest.param("2026-10-05 13:20:21", id="naive-means-ist"),
    ],
)
def test_parse_timestamp_all_forms_agree(raw):
    expected = datetime(2026, 10, 5, 13, 20, 21, tzinfo=IST)
    parsed = parse_timestamp(raw)
    assert parsed == expected
    assert parsed.tzinfo is not None


def test_utc_timestamp_crossing_midnight_lands_on_next_ist_date():
    parsed = parse_timestamp("2026-10-05T20:00:00Z")
    assert to_ist(parsed).date() == date(2026, 10, 6)


def test_to_ist_formats_with_offset():
    moment = datetime(2026, 10, 14, 12, 25, 24, tzinfo=timezone.utc)
    assert to_ist(moment).isoformat() == "2026-10-14T17:55:24+05:30"


@pytest.mark.parametrize("raw", ["", "yesterday", "2026-10-05T25:00:00Z"])
def test_parse_timestamp_invalid(raw):
    assert parse_timestamp(raw) is None
