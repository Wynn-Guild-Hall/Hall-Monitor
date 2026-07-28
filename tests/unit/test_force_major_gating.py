"""Duration gating shared by every ``~force``: janitor ceiling, monitor-only permanent."""

from datetime import timedelta

from hall_monitor.services.time_parse import gating_rejection


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


async def test_notable_survives_as_an_alias():
    """The Hall calls these major guilds now, but `~force notable` is in
    janitors' fingers and in older instructions written in the Discord.
    An alias costs nothing; the help listing shows only `major`, so
    nothing teaches the old name to anybody new."""
    import asyncio

    from hall_monitor.discord_bot import build_bot

    async def tree():
        bot = build_bot()
        await bot.load_extension("hall_monitor.discord_bot.cogs.force")
        return bot

    bot = await tree()
    force = bot.get_command("force")
    assert force.get_command("major") is force.get_command("notable")
    assert bot.get_command("unforce").get_command("notable") is not None
    assert "notable" not in {c.name for c in force.commands}, "listed as `major`"
