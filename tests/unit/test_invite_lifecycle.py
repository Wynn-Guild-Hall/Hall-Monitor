"""PendingInvite invariants — one-per-uuid, sweep revokes stale, re-request revokes old, reject-if-already-delegate."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from hall_monitor.db.models import Delegate, PendingInvite
from hall_monitor.services import discord_invites
from hall_monitor.services.discord_invites import (
    AlreadyLiveDelegate,
    INVITE_MAX_AGE_SECONDS,
    mint_invite,
    revoke_invite,
    sweep_expired,
)


def _fake_invite(code: str):
    invite = MagicMock()
    invite.code = code
    invite.url = f"https://discord.gg/{code}"
    return invite


def _fake_channel(mint_code: str = "code1"):
    channel = MagicMock()
    channel.create_invite = AsyncMock(return_value=_fake_invite(mint_code))
    return channel


def _fake_bot():
    bot = MagicMock()
    bot.http = MagicMock()
    bot.http.delete_invite = AsyncMock()
    return bot


def _guild_with_member(discord_user_id: int | None):
    """A discord.Guild-like mock whose get_member returns a live member for
    ``discord_user_id`` (or None if that id doesn't match)."""
    guild = MagicMock()
    member = MagicMock() if discord_user_id is not None else None

    def get_member(uid):
        return member if uid == discord_user_id else None

    guild.get_member = get_member
    return guild


async def test_mint_persists_pending_invite_row(db):
    channel = _fake_channel("abc123")
    row = await mint_invite("uuid-1", "VETS", 5, channel=channel)
    assert row.mc_uuid == "uuid-1"
    assert row.guild_tag == "VETS"
    assert row.roles_bits == 5
    assert row.discord_invite_code == "abc123"
    channel.create_invite.assert_awaited_once()
    kwargs = channel.create_invite.await_args.kwargs
    assert kwargs["max_uses"] == 1
    assert kwargs["max_age"] == INVITE_MAX_AGE_SECONDS


async def test_mint_enforces_one_row_per_uuid_and_revokes_prior(db):
    """Re-requesting for the same UUID revokes the old Discord invite and
    replaces the DB row with a fresh one."""
    channel = _fake_channel("first")
    bot = _fake_bot()
    await mint_invite("uuid-1", "VETS", 5, channel=channel, bot=bot)

    channel.create_invite = AsyncMock(return_value=_fake_invite("second"))
    row = await mint_invite("uuid-1", "VETS", 3, channel=channel, bot=bot)

    # DB has exactly one row for this UUID, and it's the fresh one.
    rows = await PendingInvite.filter(mc_uuid="uuid-1").all()
    assert len(rows) == 1
    assert rows[0].discord_invite_code == "second"
    assert row.roles_bits == 3

    # Prior code was revoked.
    bot.http.delete_invite.assert_awaited_once()
    args, _ = bot.http.delete_invite.await_args
    assert args[0] == "first"


async def test_mint_rejects_when_uuid_already_a_live_delegate(db):
    await Delegate.create(
        mc_uuid="uuid-1", discord_user_id=42, guild_tag="VETS"
    )
    guild = _guild_with_member(42)
    channel = _fake_channel("abc")
    with pytest.raises(AlreadyLiveDelegate):
        await mint_invite(
            "uuid-1", "VETS", 5, channel=channel, discord_guild=guild
        )
    channel.create_invite.assert_not_awaited()


async def test_mint_ignores_delegate_row_whose_member_left(db):
    """A Delegate row whose Discord user has vanished from the server must
    not block re-verification — is_current_member returns False."""
    await Delegate.create(
        mc_uuid="uuid-1", discord_user_id=42, guild_tag="VETS"
    )
    guild_without_member = _guild_with_member(None)
    channel = _fake_channel("fresh")
    row = await mint_invite(
        "uuid-1", "VETS", 1, channel=channel, discord_guild=guild_without_member
    )
    assert row.discord_invite_code == "fresh"


async def test_revoke_swallows_not_found(db):
    import discord

    bot = _fake_bot()
    bot.http.delete_invite = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "gone")
    )
    # Should not raise.
    await revoke_invite("missing-code", bot=bot)


async def test_sweep_deletes_rows_older_than_ttl(db, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.services.discord_invites.settings.pending_invite_ttl_minutes",
        45,
    )
    fresh = await PendingInvite.create(
        mc_uuid="fresh", guild_tag="VETS", roles_bits=1, discord_invite_code="fresh-c"
    )
    stale = await PendingInvite.create(
        mc_uuid="stale", guild_tag="VETS", roles_bits=1, discord_invite_code="stale-c"
    )
    # Backdate the stale row by more than the TTL.
    stale.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await stale.save()

    bot = _fake_bot()
    deleted = await sweep_expired(bot=bot)
    assert deleted == 1
    remaining = {r.mc_uuid for r in await PendingInvite.all()}
    assert remaining == {"fresh"}
    bot.http.delete_invite.assert_awaited_once()
    args, _ = bot.http.delete_invite.await_args
    assert args[0] == "stale-c"


async def test_sweep_without_bot_still_deletes_rows(db, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.services.discord_invites.settings.pending_invite_ttl_minutes",
        45,
    )
    stale = await PendingInvite.create(
        mc_uuid="stale", guild_tag="VETS", roles_bits=1, discord_invite_code="stale-c"
    )
    stale.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await stale.save()
    assert await sweep_expired() == 1
    assert await PendingInvite.filter(mc_uuid="stale").count() == 0


async def test_sweep_no_op_when_nothing_stale(db):
    await PendingInvite.create(
        mc_uuid="u", guild_tag="VETS", roles_bits=0, discord_invite_code="c"
    )
    assert await sweep_expired() == 0
    assert await PendingInvite.filter().count() == 1
