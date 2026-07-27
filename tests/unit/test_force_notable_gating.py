"""Duration gating for ``~force notable``: janitor ceiling + monitor-only permanent."""

from datetime import timedelta

from hall_monitor.discord_bot.cogs.force.notable import gating_rejection


def test_permanent_rejected_for_janitor():
    assert gating_rejection(None, is_monitor=False) is not None


def test_permanent_allowed_for_monitor():
    assert gating_rejection(None, is_monitor=True) is None


def test_short_duration_allowed_for_janitor():
    """No floor — a janitor granting a week has a reason, and it self-expires."""
    assert gating_rejection(timedelta(days=7), is_monitor=False) is None


def test_three_month_ceiling_is_inclusive_for_janitor():
    assert gating_rejection(timedelta(days=90), is_monitor=False) is None


def test_past_the_ceiling_rejected_for_janitor():
    assert gating_rejection(timedelta(days=91), is_monitor=False) is not None


def test_year_rejected_for_janitor():
    assert gating_rejection(timedelta(days=365), is_monitor=False) is not None


def test_monitor_has_no_ceiling():
    assert gating_rejection(timedelta(days=3650), is_monitor=True) is None
