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
