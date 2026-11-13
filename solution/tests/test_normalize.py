import pytest

from solution.normalize import (norm_header, parse_date, parse_decimal, parse_enum, parse_phone,
                                parse_timestamp_us, ist_date_str, us_to_ist)


@pytest.mark.parametrize("raw,want", [
    ("9876543210", "+919876543210"),
    ("+91 98765 43210", "+919876543210"),
    ("09876543210", "+919876543210"),
    ("91-98765-43210", "+919876543210"),
    ("(+91) 9876543210", "+919876543210"),
    ("+919876543210", "+919876543210"),
    ("5876543210", None),            # bad prefix
    ("987654321", None),             # 9 digits
    ("022-2345-6789", None),         # landline
    ("N/A", None),
    ("+1 415 555 0134", None),
    ("9198765432", "+919198765432"), # 10 digits starting 91 is NOT a country code
    ("919876543210 ", "+919876543210"),
    ("", None),
    (None, None),
    ("0919876543210", None),         # 13 digits
])
def test_phone(raw, want):
    assert parse_phone(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("2026-10-05", "2026-10-05"),
    ("05/10/2026", "2026-10-05"),        # day-first
    ("05-10-2026", "2026-10-05"),
    ("01-Dec-2026", "2026-12-01"),
    ("01-dec-2026", "2026-12-01"),
    ("01-DEC-2026", "2026-12-01"),
    ("31/02/2026", None),
    ("2026-13-01", None),
    ("13/13/2026", None),
    ("29/02/2028", "2028-02-29"),
    ("29/02/2027", None),
    ("soon", None), ("TBD", None), ("01-Foo-2026", None), ("", None),
])
def test_date(raw, want):
    assert parse_date(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("27554", "27554.00"), ("₹27,554", "27554.00"), ("Rs. 24787", "24787.00"), ("Rs 1,000", "1000.00"),
    ("INR 25301.4", "25301.40"), ("inr25301.456", "25301.46"), ("rs.5", "5.00"), ("0", "0.00"),
    ("₹ 1,23,456.789", "123456.79"),
    ("-5", None), ("N/A", None), ("-", None), ("twelve thousand", None), ("", None), ("₹", None),
])
def test_decimal(raw, want):
    assert parse_decimal(raw) == want


def test_enum():
    v = ("en", "hi")
    assert parse_enum("HI", v) == "hi"
    assert parse_enum(" en ", v) == "en"
    assert parse_enum("Hindi", v) is None


def test_header_normalisation():
    assert norm_header("  Mobile_No ") == "mobile no"
    assert norm_header("Policy__  No") == "policy no"
    assert norm_header("PHONE") == "phone"


def test_timestamps_and_ist_dates():
    z = parse_timestamp_us("2026-10-05T13:20:21Z")
    off = parse_timestamp_us("2026-10-05T18:50:21+05:30")
    naive = parse_timestamp_us("2026-10-05 18:50:21")   # naive == IST
    assert z == off == naive
    assert us_to_ist(z).isoformat() == "2026-10-05T18:50:21+05:30"
    # 20:00Z on the 5th is already the 6th in IST
    assert ist_date_str(parse_timestamp_us("2026-10-05T20:00:00Z")) == "2026-10-06"
    assert parse_timestamp_us("garbage") is None
