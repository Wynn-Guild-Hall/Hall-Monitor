"""Persistent MC UUID ↔ Discord user bindings for guild representatives.

``left_at`` is the DB-side "member still in the server?" tri-state:

- ``NULL``  — believed to still be a member.
- non-NULL — the ``on_member_remove`` listener saw them leave (Stage 11+).

``is_current_member`` cross-checks against Discord when a bot guild is
available, so the DB not being caught up yet doesn't produce a false
positive between the leave event and the listener firing.
"""

from datetime import datetime, timezone

import discord

from hall_monitor.db.models import Delegate


async def get_by_mc_uuid(mc_uuid: str) -> Delegate | None:
    return await Delegate.get_or_none(mc_uuid=mc_uuid)


async def get_by_discord_user_id(discord_user_id: int) -> Delegate | None:
    return await Delegate.get_or_none(discord_user_id=discord_user_id)


async def register(mc_uuid: str, discord_user_id: int, guild_tag: str) -> Delegate:
    """Create or reactivate a Delegate row for this MC UUID.

    Reactivation clears ``left_at`` so a returning delegate isn't confused
    with a stale one.
    """
    delegate, _ = await Delegate.update_or_create(
        mc_uuid=mc_uuid,
        defaults={
            "discord_user_id": discord_user_id,
            "guild_tag": guild_tag,
            "left_at": None,
        },
    )
    return delegate


async def mark_left(discord_user_id: int) -> None:
    await Delegate.filter(discord_user_id=discord_user_id).update(
        left_at=datetime.now(timezone.utc)
    )


async def is_current_member(
    mc_uuid: str, discord_guild: discord.Guild | None = None
) -> bool:
    """True iff a Delegate row exists for this UUID and the user is still
    in ``discord_guild``. Falls back to the DB ``left_at`` field when no
    guild handle is available (tests, or the bot not being connected yet)."""
    delegate = await get_by_mc_uuid(mc_uuid)
    if delegate is None or delegate.left_at is not None:
        return False
    if discord_guild is None:
        return True
    return discord_guild.get_member(delegate.discord_user_id) is not None
