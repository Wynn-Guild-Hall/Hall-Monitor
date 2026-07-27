"""``~script`` dispatch plus the refresh_notability script's reporting.

The script is the supported way to trigger a notability sweep: doing it
from a second process contends with the bot for the SQLite write lock.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from hall_monitor.discord_bot.cogs.admin.scripts import _loader, refresh_notability
from hall_monitor.services import notability
from hall_monitor.services.notability import RefreshSummary


def _fake_ctx():
    """A Context-like mock whose reply returns an editable message."""
    message = MagicMock()
    message.edit = AsyncMock()
    ctx = MagicMock()
    ctx.reply = AsyncMock(return_value=message)
    return ctx, message


def _edits(message) -> list[str]:
    return [call.kwargs["content"] for call in message.edit.await_args_list]


def test_script_is_discoverable():
    assert "refresh_notability" in set(_loader.discover_scripts())


def test_private_modules_are_not_scripts():
    assert "_loader" not in set(_loader.discover_scripts())


async def test_unknown_script_name_is_reported():
    ctx, _ = _fake_ctx()
    ctx.send = AsyncMock()
    await _loader.run_script("no_such_script", ctx)
    ctx.send.assert_awaited_once()
    assert "unknown script" in ctx.send.await_args.args[0]


async def test_refresh_script_reports_the_summary(monkeypatch):
    async def fake_refresh(*, on_progress=None):
        await on_progress(1, 2)
        await on_progress(2, 2)
        return RefreshSummary(evaluated=2, failed=0, notable=1, seconds=12.3)

    monkeypatch.setattr(notability, "refresh_all", fake_refresh)
    monkeypatch.setattr(notability, "is_refreshing", lambda: False)

    ctx, message = _fake_ctx()
    await refresh_notability.main(ctx)

    edits = _edits(message)
    assert "2/2 guilds" in edits[-2]
    assert "12s" in edits[-1]
    assert "2 guilds evaluated, 1 notable" in edits[-1]
    assert "failed" not in edits[-1], "a clean sweep shouldn't mention failures"


async def test_refresh_script_surfaces_failures(monkeypatch):
    async def fake_refresh(*, on_progress=None):
        return RefreshSummary(evaluated=8, failed=2, notable=3, seconds=5.0)

    monkeypatch.setattr(notability, "refresh_all", fake_refresh)
    monkeypatch.setattr(notability, "is_refreshing", lambda: False)

    ctx, message = _fake_ctx()
    await refresh_notability.main(ctx)
    assert "2 failed" in _edits(message)[-1]


async def test_refresh_script_throttles_its_edits(monkeypatch):
    """One edit per guild would blow through Discord's edit rate limit, so
    only the final tick is guaranteed to land."""
    async def fake_refresh(*, on_progress=None):
        for done in range(1, 51):
            await on_progress(done, 50)
        return RefreshSummary(evaluated=50, failed=0, notable=5, seconds=1.0)

    monkeypatch.setattr(notability, "refresh_all", fake_refresh)
    monkeypatch.setattr(notability, "is_refreshing", lambda: False)

    ctx, message = _fake_ctx()
    await refresh_notability.main(ctx)

    # 50 progress callbacks, but the interval hasn't elapsed: only the
    # completion tick plus the final summary get through.
    assert len(_edits(message)) == 2
    assert "50/50 guilds" in _edits(message)[0]


async def test_refresh_script_declines_when_one_is_running(monkeypatch):
    monkeypatch.setattr(notability, "is_refreshing", lambda: True)
    called = False

    async def fake_refresh(*, on_progress=None):
        nonlocal called
        called = True

    monkeypatch.setattr(notability, "refresh_all", fake_refresh)

    ctx, message = _fake_ctx()
    await refresh_notability.main(ctx)

    assert not called, "must not start a second sweep"
    assert "already running" in ctx.reply.await_args.args[0]
    message.edit.assert_not_awaited()


async def test_refresh_script_handles_losing_the_race(monkeypatch):
    """is_refreshing was False at the check but the scheduler got in first."""
    monkeypatch.setattr(notability, "is_refreshing", lambda: False)

    async def fake_refresh(*, on_progress=None):
        return None

    monkeypatch.setattr(notability, "refresh_all", fake_refresh)

    ctx, message = _fake_ctx()
    await refresh_notability.main(ctx)
    assert "already running" in _edits(message)[-1]


# --------------------------------------------------------------------------
# notability_breakdown
# --------------------------------------------------------------------------


async def _cache(tag: str, notable: bool, **signals):
    from hall_monitor.db.models import NotabilityCache

    import json as _json

    full = {name: signals.get(name, False) for name in (
        "top25_average_online", "level_100_plus", "season_placement",
        "territory_ownership", "war_count", "force_override",
    )}
    await NotabilityCache.create(
        guild_tag=tag, is_notable=notable, signals_json=_json.dumps(full)
    )


def _table(body: str) -> dict[str, tuple[int, int]]:
    """Parse the code-block table into {signal: (any, only)}.

    Asserting on parsed numbers rather than column positions keeps these
    tests about the counting, not the padding.
    """
    block = body.split("```")[1]
    parsed = {}
    for line in block.strip().splitlines()[1:]:  # skip header
        name, any_count, only_count = line.split()
        parsed[name] = (int(any_count), int(only_count))
    return parsed


async def test_breakdown_counts_any_versus_sole_cause(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_breakdown

    await _cache("WYNN", True, level_100_plus=True)
    await _cache("AAAB", True, level_100_plus=True)
    await _cache("VETS", True, level_100_plus=True, top25_average_online=True)
    await _cache("SMLL", False)

    ctx, _ = _fake_ctx()
    await notability_breakdown.main(ctx)
    body = ctx.reply.await_args.args[0]

    assert "4 cached, 3 notable" in body
    table = _table(body)
    assert table["level_100_plus"] == (3, 2)
    # top25 corroborates VETS but is never the sole reason for anything.
    assert table["top25_average_online"] == (1, 0)
    assert "sole cause: 2 · multiple: 1 · unexplained: 0" in body


async def test_breakdown_ranks_the_criterion_to_tighten_first(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_breakdown

    await _cache("AAAB", True, season_placement=True)
    for tag in ("WYNN", "AAAC", "AAAD"):
        await _cache(tag, True, level_100_plus=True)

    ctx, _ = _fake_ctx()
    await notability_breakdown.main(ctx)
    body = ctx.reply.await_args.args[0]

    rows = [l for l in body.splitlines() if l.startswith(("level_", "season_"))]
    assert rows[0].startswith("level_100_plus"), "highest sole-cause count leads"


async def test_breakdown_treats_null_as_not_met(db):
    """A skipped signal is `null`, which must not be counted as satisfied."""
    from hall_monitor.db.models import NotabilityCache
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_breakdown

    await NotabilityCache.create(
        guild_tag="VETS",
        is_notable=True,
        signals_json='{"top25_average_online": true, "war_count": null}',
    )
    ctx, _ = _fake_ctx()
    await notability_breakdown.main(ctx)
    body = ctx.reply.await_args.args[0]
    assert _table(body)["war_count"] == (0, 0)
    assert _table(body)["top25_average_online"] == (1, 1)
    assert "sole cause: 1" in body


async def test_breakdown_lists_sole_cause_guilds(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_breakdown

    await _cache("WYNN", True, level_100_plus=True)
    await _cache("AAAB", True, level_100_plus=True, war_count=True)

    ctx, _ = _fake_ctx()
    await notability_breakdown.main(ctx, "level_100_plus")
    body = ctx.reply.await_args.args[0]
    assert "**1** guilds are notable on `level_100_plus` alone" in body
    assert "WYNN" in body and "AAAB" not in body


async def test_breakdown_rejects_an_unknown_signal(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_breakdown

    await _cache("WYNN", True, level_100_plus=True)
    ctx, _ = _fake_ctx()
    await notability_breakdown.main(ctx, "wars_lol")
    assert "unknown signal" in ctx.reply.await_args.args[0]


async def test_breakdown_says_so_when_the_cache_is_empty(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_breakdown

    ctx, _ = _fake_ctx()
    await notability_breakdown.main(ctx)
    assert "refresh_notability" in ctx.reply.await_args.args[0]
