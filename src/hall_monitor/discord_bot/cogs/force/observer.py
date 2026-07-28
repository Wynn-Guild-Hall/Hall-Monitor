"""``~force observer`` — invite somebody who represents nobody.

The Hall is a room of guild representatives, and an observer is the
exception a janitor makes by hand: a Wynncraft admin, a partner
community's organiser, somebody helping run an event. They get in and
they get to read, and that's all — no delegate standing, no contact
slots, no guild colour, no tag on their nickname.

Everything downstream already handles them by their **not having a
``Delegate`` row**, which is the whole trick. The guild watch iterates
delegates; the reconcile settles delegates; the roster names contacts;
``nicknames.enforce`` returns early for anyone the Hall has no row for
(and again, explicitly, for the observer role). So an observer is
invisible to every hourly pass rather than being a special case inside
each one.

The invite is still minted against a **Minecraft** account, because
that's what ``PendingInvite`` is keyed on and what makes "who was this?"
answerable a year later. It proves nothing about them — no chief check,
no guild — the janitor's word is the whole of the authority, which is
why this is janitor-gated and logged at warning level.

It lives a **week** rather than the MC flow's ten minutes: a janitor has
to paste it to a human and then wait for them
(``discord_invites.OBSERVER_INVITE_MAX_AGE_SECONDS``).
"""

import logging

from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.db.models import PendingInvite
from hall_monitor.discord_bot.permissions import is_janitor
from hall_monitor.external import resolve_profile
from hall_monitor.services import delegate_registry, discord_invites, role_bits

logger = logging.getLogger(__name__)

# `PendingInvite.guild_tag` isn't nullable and an observer has no guild.
# Wynncraft's reserved "Nobody" tag is honest about that and keeps the
# column meaningful — the same borrowing as the blank banner (§15.4).
OBSERVER_TAG = "NONE"


async def invite(ctx: commands.Context, username: str) -> str:
    """Mint an observer invite and return what to say about it."""
    if ctx.guild is None:
        return "run this in the server — I need a channel to invite to."
    if not settings.observer_role_id:
        return (
            "`OBSERVER_ROLE_ID` isn't set, so an observer would arrive and get "
            "no role at all. That's a deploy setting — ask a monitor."
        )
    channel = ctx.guild.get_channel(settings.welcome_channel_id)
    if channel is None:
        return "I can't reach the welcome channel, so I can't mint an invite."

    profile = await resolve_profile(username, urgent=True)
    if profile is None:
        return f"`{username}` isn't a Minecraft account I could find."

    if await delegate_registry.get_by_mc_uuid(profile.uuid) is not None:
        # Not fatal to anything, but almost certainly a mistake — and the
        # join listener would give them the observer role instead of the
        # representative one they already earned.
        return (
            f"`{profile.username}` is already a registered representative. "
            "Observers are for people who speak for no guild."
        )

    try:
        pending = await discord_invites.mint_invite(
            profile.uuid,
            OBSERVER_TAG,
            role_bits.OBSERVER,
            channel=channel,
            bot=ctx.bot,
            discord_guild=ctx.guild,
            mc_username=profile.username,
            max_age=discord_invites.OBSERVER_INVITE_MAX_AGE_SECONDS,
        )
    except discord_invites.AlreadyLiveDelegate:
        return f"`{profile.username}` is already in the Hall."

    logger.warning(
        "observer: %s (%s) invited %s as an observer",
        ctx.author,
        getattr(ctx.author, "id", None),
        profile.username,
    )
    return (
        f"Observer invite for `{profile.username}`: "
        f"https://discord.gg/{pending.discord_invite_code}\n"
        "Single use, good for a week. They'll get the observer role and "
        "nothing else — no guild, no contact slots, no tag on their name."
    )


async def revoke(ctx: commands.Context, username: str) -> str:
    """Cancel an observer invite that hasn't been used yet."""
    profile = await resolve_profile(username, urgent=True)
    if profile is None:
        return f"`{username}` isn't a Minecraft account I could find."

    row = await PendingInvite.get_or_none(mc_uuid=profile.uuid)
    if row is None:
        return f"there's no outstanding invite for `{profile.username}`."
    if not role_bits.is_observer(row.roles_bits):
        # Refused rather than revoked. That's somebody's verification in
        # flight, and cancelling it from *this* command would make a typo
        # cost them a re-verification for no reason they could see.
        return (
            f"`{profile.username}` has a *representative* invite outstanding, "
            "not an observer one. Leave it for the sweep, or ask them to "
            "request a fresh code."
        )

    await discord_invites.revoke_invite(row.discord_invite_code, bot=ctx.bot)
    await row.delete()
    logger.warning(
        "observer: %s (%s) revoked the observer invite for %s",
        ctx.author,
        getattr(ctx.author, "id", None),
        profile.username,
    )
    return f"revoked the observer invite for `{profile.username}`."


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="observer")
    @is_janitor()
    async def force_observer(ctx: commands.Context, username: str) -> None:
        """invite somebody to observe, representing no guild"""
        await ctx.reply(await invite(ctx, username))

    @cog.unforce.command(name="observer")
    @is_janitor()
    async def unforce_observer(ctx: commands.Context, username: str) -> None:
        """cancel an observer invite that hasn't been used"""
        await ctx.reply(await revoke(ctx, username))
