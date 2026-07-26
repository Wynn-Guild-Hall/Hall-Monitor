"""Duration gating for ``~force notable``: janitor floor + monitor-only permanent."""

from datetime import timedelta

from hall_monitor.discord_bot.cogs.force.notable import gating_rejection


def test_permanent_rejected_for_janitor():
    assert gating_rejection(None, is_monitor=False) is not None


def test_permanent_allowed_for_monitor():
    assert gating_rejection(None, is_monitor=True) is None


def test_short_duration_rejected_for_janitor():
    assert gating_rejection(timedelta(days=30), is_monitor=False) is not None


def test_short_duration_allowed_for_monitor():
    assert gating_rejection(timedelta(days=30), is_monitor=True) is None


def test_two_month_floor_accepted_for_janitor():
    assert gating_rejection(timedelta(days=60), is_monitor=False) is None


def test_longer_than_floor_accepted_for_janitor():
    assert gating_rejection(timedelta(days=90), is_monitor=False) is None
