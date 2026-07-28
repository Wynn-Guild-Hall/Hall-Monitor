"""``~force notable`` — override a guild's notability status for a duration.

Janitors get a three-month ceiling: long enough to carry a guild through
a quiet patch, short enough that nobody can quietly park a guild in the
Hall forever. Monitors have no ceiling and can pass ``0`` for a permanent
override. There's no floor — a janitor who wants to grant a week has a
reason, and a short override expires on its own anyway.
"""

from datetime import datetime, timezone

from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.db.models import ForceOverride
from hall_monitor.discord_bot.permissions import is_janitor
from hall_monitor.services.time_parse import (
    InvalidDuration,
    gating_rejection,
    parse as parse_duration,
)


def _is_monitor(ctx: commands.Context) -> bool:
    if ctx.guild is None or not hasattr(ctx.author, "roles"):
        return False
    return any(r.id == settings.monitor_role_id for r in ctx.author.roles)


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="notable")
    @is_janitor()
    async def force_notable(
        ctx: commands.Context, guild_tag: str, duration: str
    ) -> None:
        """treat a guild as notable for a while, whatever the signals say"""
        try:
            delta = parse_duration(duration)
        except InvalidDuration:
            await ctx.reply(
                f"couldn't parse `{duration}` — expected e.g. `3mo`, `7d`, or `0` (monitor only)."
            )
            return

        rejection = gating_rejection(delta, _is_monitor(ctx))
        if rejection is not None:
            await ctx.reply(rejection)
            return

        expires_at = None if delta is None else datetime.now(timezone.utc) + delta
        # Match an existing row case-insensitively before creating one:
        # `~force notable vets` and `~force notable VETS` are the same
        # guild, and two rows would mean re-forcing silently did nothing.
        existing = await ForceOverride.filter(
            kind="notable", subject__iexact=guild_tag
        ).first()
        if existing is None:
            await ForceOverride.create(
                kind="notable",
                subject=guild_tag,
                expires_at=expires_at,
                payload_json="{}",
            )
        else:
            existing.expires_at = expires_at
            await existing.save()
        window = "permanently" if expires_at is None else f"until {expires_at.isoformat(timespec='minutes')}"
        await ctx.reply(f"forced `{guild_tag}` notable {window}.")

    @cog.unforce.command(name="notable")
    @is_janitor()
    async def unforce_notable(ctx: commands.Context, guild_tag: str) -> None:
        """drop a notability override and go back to the signals"""
        deleted = await ForceOverride.filter(
            kind="notable", subject__iexact=guild_tag
        ).delete()
        if deleted:
            await ctx.reply(f"cleared notable override on `{guild_tag}`.")
        else:
            await ctx.reply(f"no notable override on `{guild_tag}` to clear.")
