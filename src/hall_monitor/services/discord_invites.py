"""Single-use Discord invite lifecycle.

Enforces the ``PendingInvite`` invariants:

- **One row per MC UUID.** Re-requesting revokes the prior Discord
  invite and mints a fresh one.
- **Zero rows if the UUID already has a live Delegate.** MC-time verify
  raises :class:`AlreadyLiveDelegate` so the caller can return a
  "you're already in" kick message.
- **Expiry sweep.** ``sweep_expired`` deletes rows older than
  :attr:`settings.pending_invite_ttl_minutes` and revokes their Discord
  invites — belt-and-braces against the bot going down mid-flow.

Discord's HTTP surface is passed in as ``channel`` / ``bot`` so this
module stays framework-thin and testable with plain mocks.
"""

import logging
from datetime import datetime, timedelta, timezone

import discord

from hall_monitor.config import settings
from hall_monitor.db.models import PendingInvite
from hall_monitor.services import delegate_registry

logger = logging.getLogger(__name__)

INVITE_MAX_AGE_SECONDS = 600


class AlreadyLiveDelegate(Exception):
    """Signals that mint_invite was called for a MC UUID whose Discord
    user is already in the server as a delegate."""


async def mint_invite(
    mc_uuid: str,
    guild_tag: str,
    roles_bits: int,
    *,
    channel: discord.abc.GuildChannel,
    bot: discord.Client | None = None,
    discord_guild: discord.Guild | None = None,
) -> PendingInvite:
    """Mint a single-use Discord invite bound to a UUID and return the row.

    ``bot`` is optional; without it we can still create the new invite
    but can't ask Discord to delete a prior one — the sweep will collect
    it after TTL. Tests exercise both paths.
    """
    if await delegate_registry.is_current_member(mc_uuid, discord_guild=discord_guild):
        raise AlreadyLiveDelegate(mc_uuid)

    prior = await PendingInvite.get_or_none(mc_uuid=mc_uuid)
    if prior is not None:
        if bot is not None:
            await revoke_invite(prior.discord_invite_code, bot=bot)
        await prior.delete()

    invite = await channel.create_invite(
        max_uses=1,
        max_age=INVITE_MAX_AGE_SECONDS,
        reason=f"hall-monitor: {guild_tag} verification for {mc_uuid}",
    )
    return await PendingInvite.create(
        mc_uuid=mc_uuid,
        guild_tag=guild_tag,
        roles_bits=roles_bits,
        discord_invite_code=invite.code,
    )


async def revoke_invite(code: str, *, bot: discord.Client) -> None:
    """Delete a Discord invite by code. Idempotent — a 404 is fine."""
    try:
        await bot.http.delete_invite(code, reason="hall-monitor: revoke")
    except discord.NotFound:
        pass


async def sweep_expired(*, bot: discord.Client | None = None) -> int:
    """Delete PendingInvite rows past their TTL and revoke their Discord
    invites. Returns the count deleted for logging.

    ``bot`` is optional so scheduler binding stays flexible; without it
    the DB rows still get cleaned but the Discord-side invites will just
    expire naturally (10-minute ``max_age`` we set on mint)."""
    threshold = datetime.now(timezone.utc) - timedelta(
        minutes=settings.pending_invite_ttl_minutes
    )
    stale = await PendingInvite.filter(created_at__lt=threshold).all()
    for row in stale:
        if bot is not None:
            try:
                await revoke_invite(row.discord_invite_code, bot=bot)
            except Exception:
                logger.exception(
                    "sweep: failed to revoke Discord invite %s", row.discord_invite_code
                )
        await row.delete()
    return len(stale)


async def resolve_used_invite(member) -> None:
    """Match a joining member to their ``PendingInvite`` and apply bound roles."""
    raise NotImplementedError  # Stage 6
