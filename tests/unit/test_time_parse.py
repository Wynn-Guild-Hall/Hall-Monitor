"""Duration parser used by every ``~force`` command."""

from datetime import timedelta

import pytest

from hall_monitor.services.time_parse import InvalidDuration, parse


def test_days():
    assert parse("7d") == timedelta(days=7)


def test_weeks():
    assert parse("2w") == timedelta(days=14)


def test_months_uses_30_day_month():
    assert parse("3mo") == timedelta(days=90)


def test_years():
    assert parse("1y") == timedelta(days=365)


def test_case_insensitive_and_whitespace_tolerant():
    assert parse(" 2 W ") == timedelta(days=14)


def test_zero_returns_permanent_sentinel():
    assert parse("0") is None


def test_rejects_empty():
    with pytest.raises(InvalidDuration):
        parse("")


def test_rejects_unknown_unit():
    with pytest.raises(InvalidDuration):
        parse("5h")


def test_rejects_bare_month_letter():
    """``m`` alone is not a supported unit — only ``mo``."""
    with pytest.raises(InvalidDuration):
        parse("3m")


def test_rejects_negative_quantity():
    with pytest.raises(InvalidDuration):
        parse("-1d")


def test_rejects_zero_with_unit():
    with pytest.raises(InvalidDuration):
        parse("0d")
