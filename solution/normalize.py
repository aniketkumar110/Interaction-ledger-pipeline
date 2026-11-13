"""Pure value-normalisation functions (no I/O). These are the spec's rules."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))
_WS_US = re.compile(r"[_\s]+")


def norm_header(s: str) -> str:
    return _WS_US.sub(" ", s.strip().lower()).strip()


# ---------------------------------------------------------------- phone
def parse_phone(raw) -> str | None:
    """Return canonical '+91XXXXXXXXXX' or None if invalid."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw), flags=re.ASCII)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


# ---------------------------------------------------------------- date
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_RE_YMD = re.compile(r"(\d{4})-(\d{2})-(\d{2})", re.ASCII)
_RE_DMY = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", re.ASCII)
_RE_DMONY = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", re.ASCII)


def parse_date(raw: str) -> str | None:
    """Day-first. Returns ISO 'YYYY-MM-DD' or None."""
    s = raw.strip()
    try:
        if m := _RE_YMD.fullmatch(s):
            y, mo, d = map(int, m.groups())
        elif m := _RE_DMY.fullmatch(s):
            d, mo, y = map(int, m.groups())
        elif m := _RE_DMONY.fullmatch(s):
            d, y = int(m.group(1)), int(m.group(3))
            mo = _MONTHS.get(m.group(2).lower())
            if mo is None:
                return None
        else:
            return None
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------- decimal
_RE_MONEY = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?|\.\d+)", re.IGNORECASE)


def parse_decimal(raw: str) -> str | None:
    """Money -> string with exactly 2 decimals, or None if invalid / negative."""
    m = _RE_MONEY.fullmatch(raw.strip())
    if not m:
        return None
    try:
        v = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return str(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- enum
def parse_enum(raw: str, values: tuple[str, ...]) -> str | None:
    v = raw.strip().lower()
    return v if v in {x.lower() for x in values} else None


# ---------------------------------------------------------------- timestamps
def parse_timestamp_us(raw) -> int | None:
    """ISO-8601 -> integer microseconds since epoch (UTC). Naive means IST."""
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def us_to_ist(ts_us: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc).astimezone(IST) + timedelta(microseconds=ts_us)


def ist_date_str(ts_us: int) -> str:
    return us_to_ist(ts_us).date().isoformat()
