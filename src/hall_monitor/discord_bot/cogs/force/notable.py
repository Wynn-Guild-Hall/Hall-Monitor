"""``~force notable`` — override a guild's notability status for a duration.

Janitors get a two-month floor on the duration (long enough to matter,
short enough to expire on its own). Monitors can pass ``0`` for a
permanent override.
"""

from datetime import datetime, timedelta, timezone

from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.db.models import ForceOverride
from hall_monitor.discord_bot.permissions import is_janitor
from hall_monitor.services.time_parse import InvalidDuration, parse as parse_duration

_JANITOR_MIN = timedelta(days=60)


def gating_rejection(delta: timedelta | None, is_monitor: bool) -> str | None:
    """Pure permissions check for the parsed duration.

    Returns a user-facing rejection message when the caller isn't allowed to
    use this duration, or ``None`` when the request should proceed.
    Extracted from the command handler so it's unit-testable without
    faking a Discord context.
    """
    if delta is None and not is_monitor:
        return "permanent overrides are monitor-only; try `2mo` or longer."
    if delta is not None and delta < _JANITOR_MIN and not is_monitor:
        return (
            "janitor overrides must be at least two months (`2mo`); "
            "monitors can go shorter."
        )
    return None


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
        await ForceOverride.update_or_create(
            kind="notable",
            subject=guild_tag,
            defaults={"expires_at": expires_at, "payload_json": "{}"},
        )
        window = "permanently" if expires_at is None else f"until {expires_at.isoformat(timespec='minutes')}"
        await ctx.reply(f"forced `{guild_tag}` notable {window}.")

    @cog.unforce.command(name="notable")
    @is_janitor()
    async def unforce_notable(ctx: commands.Context, guild_tag: str) -> None:
        deleted = await ForceOverride.filter(
            kind="notable", subject=guild_tag
        ).delete()
        if deleted:
            await ctx.reply(f"cleared notable override on `{guild_tag}`.")
        else:
            await ctx.reply(f"no notable override on `{guild_tag}` to clear.")
