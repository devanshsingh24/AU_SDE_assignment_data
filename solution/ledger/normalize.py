"""Pure normalisation functions for lead fields and event timestamps.

Every function here is side-effect free: no database, no file I/O. Each one
takes a raw string and returns either the canonical value or ``None`` when the
value is invalid. Deciding whether a value is *missing* (empty after trimming)
is the caller's job, because missing and invalid lead to different outcomes
(``missing_required`` vs ``invalid_phone`` / field warning).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))

_HEADER_SEPARATORS = re.compile(r"[\s_]+")
_NON_DIGITS = re.compile(r"\D")

# Day-first formats only. Regexes (instead of strptime) keep parsing strict and
# independent of the OS locale for month names.
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DMY_DATE = re.compile(r"(\d{2})([/-])(\d{2})\2(\d{4})")
_DMONY_DATE = re.compile(r"(\d{2})-([A-Za-z]{3})-(\d{4})")
_MONTHS = {
    name: i
    for i, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}

# Longest prefixes first so "rs." is not consumed as "rs" leaving a stray dot.
_CURRENCY_PREFIX = re.compile(r"^(?:₹|inr|rs\.|rs)\s*", re.IGNORECASE)
_PLAIN_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_TWO_PLACES = Decimal("0.01")


def normalize_header(header: str) -> str:
    """Lower-case, trim, and collapse runs of ``_``/whitespace to one space.

    Spec: Lead files › Header matching. The result is trimmed once more so a
    header like ``"phone_"`` does not keep a trailing space.
    """
    return _HEADER_SEPARATORS.sub(" ", header.strip().lower()).strip()


def normalize_phone(raw: str) -> str | None:
    """Indian mobile → ``+91XXXXXXXXXX``, or ``None`` if invalid.

    Spec: Lead files › phone. Strip non-digits, drop a leading ``91`` from 12
    digits or a leading ``0`` from 11 digits, then require 10 digits starting
    with 6-9.
    """
    digits = _NON_DIGITS.sub("", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return None


def parse_date(raw: str) -> date | None:
    """Parse ``YYYY-MM-DD``, ``DD/MM/YYYY``, ``DD-MM-YYYY`` or ``DD-Mon-YYYY``.

    Spec: Lead files › date. Day-first; impossible dates such as 31/02 are
    invalid. Month names are case-insensitive.
    """
    value = raw.strip()
    try:
        if m := _ISO_DATE.fullmatch(value):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := _DMY_DATE.fullmatch(value):
            return date(int(m[4]), int(m[3]), int(m[1]))
        if m := _DMONY_DATE.fullmatch(value):
            month = _MONTHS.get(m[2].lower())
            if month is None:
                return None
            return date(int(m[3]), month, int(m[1]))
    except ValueError:  # impossible calendar date, e.g. 31/02 or month 13
        return None
    return None


def parse_decimal(raw: str) -> Decimal | None:
    """Parse an amount like ``₹12,345``, ``Rs. 500`` or ``INR 1200.5``.

    Spec: Lead files › decimal. Optional currency prefix, commas are thousands
    separators, must be >= 0, rounded half-up to 2 places.
    """
    value = _CURRENCY_PREFIX.sub("", raw.strip()).replace(",", "")
    # The plain-number pattern has no sign, so negatives are rejected here.
    if not _PLAIN_NUMBER.fullmatch(value):
        return None
    try:
        return Decimal(value).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def parse_enum(raw: str, allowed: list[str]) -> str | None:
    """Case-insensitive match against ``allowed``; returns the lower-case value.

    Spec: Lead files › enum.
    """
    value = raw.strip().lower()
    return value if value in {v.lower() for v in allowed} else None


def parse_timestamp(raw: str) -> datetime | None:
    """Parse an event timestamp into a timezone-aware datetime.

    Spec: Event files › Timestamps. Accepts UTC (``...Z``), offset
    (``...+05:30``) and naive values; naive means IST.
    """
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


def to_ist(moment: datetime) -> datetime:
    """Convert an aware datetime to IST (all report dates are IST dates)."""
    return moment.astimezone(IST)
