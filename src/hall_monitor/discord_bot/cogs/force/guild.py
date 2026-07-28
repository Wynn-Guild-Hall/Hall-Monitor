"""``~force guild`` — override where the Hall thinks a member is playing.

This is the counterweight to the hourly guild watch. Wynncraft is the
authority on who's in which guild, and mostly that's what you want, but
it can be wrong for us in both directions: a rep mid-transfer flickers
between guilds, an alt account shows the wrong one, and a shared account
shows whoever logged in last. A janitor who knows better says so here.

The override sits in front of ``Delegate.current_guild_tag`` rather than
in it, because the watch rewrites that column every hour — a forced value
stored there would survive exactly until the next sweep. Forcing the
guild a member already represents is the useful common case: it undoes an
incorrect External Relegate.

Note this is *not* the same as re-representing a different guild, which
changes who they speak for and needs `~force rep` (Stage 15). This only
changes where they're seen to be playing.
"""

from datetime import datetime, timezone

import discord
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.discord_bot.permissions import has_any_role, is_janitor
from hall_monitor.services import delegate_registry, nicknames, notability, transitions
from hall_monitor.services.time_parse import (
    InvalidDuration,
    gating_rejection,
    parse as parse_duration,
)


async def apply_now(ctx: commands.Context, user: discord.Member) -> None:
    """Settle the target's standing, roles and nickname immediately.

    The hourly reconcile would get there anyway; waiting an hour to find
    out whether a command did what you meant is what makes an override
    feel broken.
    """
    delegate = await delegate_registry.get_by_discord_user_id(user.id)
    if delegate is None or ctx.guild is None:
        return
    notable = await notability.is_notable(delegate.guild_tag)
    await transitions.settle_members(ctx.guild, delegate.guild_tag, notable=notable)
    await nicknames.enforce(user, reason=f"hall-monitor: ~force guild by {ctx.author}")


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="guild")
    @is_janitor()
    async def force_guild(
        ctx: commands.Context, user: discord.Member, guild_tag: str, duration: str
    ) -> None:
        """treat a member as playing for a guild, whatever Wynncraft says"""
        try:
            delta = parse_duration(duration)
        except InvalidDuration:
            await ctx.reply(
                f"couldn't parse `{duration}` — expected e.g. `3mo`, `7d`, "
                "or `0` (monitor only)."
            )
            return

        rejection = gating_rejection(
            delta, has_any_role(ctx, settings.monitor_role_id)
        )
        if rejection is not None:
            await ctx.reply(rejection)
            return

        expires_at = None if delta is None else datetime.now(timezone.utc) + delta
        await delegate_registry.set_forced_guild(user.id, guild_tag, expires_at)
        await apply_now(ctx, user)

        delegate = await delegate_registry.get_by_discord_user_id(user.id)
        window = (
            "permanently"
            if expires_at is None
            else f"until {expires_at.isoformat(timespec='minutes')}"
        )
        note = (
            ""
            if delegate is not None
            else " They aren't a registered delegate, so nothing changes for "
            "them until they verify."
        )
        await ctx.reply(
            f"{user.mention} is treated as playing for `{guild_tag}` {window}.{note}"
        )

    @cog.unforce.command(name="guild")
    @is_janitor()
    async def unforce_guild(ctx: commands.Context, user: discord.Member) -> None:
        """drop a guild override and go back to what Wynncraft says"""
        cleared = await delegate_registry.clear_forced_guild(user.id)
        if not cleared:
            await ctx.reply(f"no guild override on {user.mention} to clear.")
            return
        await apply_now(ctx, user)
        await ctx.reply(
            f"cleared the guild override on {user.mention} — back to whatever "
            "the hourly watch sees."
        )
