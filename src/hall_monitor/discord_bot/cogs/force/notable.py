"""``~force notable`` — override a guild's notability status for a duration.

Janitors get a three-month ceiling: long enough to carry a guild through
a quiet patch, short enough that nobody can quietly park a guild in the
Hall forever. Monitors have no ceiling and can pass ``0`` for a permanent
override. There's no floor — a janitor who wants to grant a week has a
reason, and a short override expires on its own anyway.
"""

from datetime import datetime, timedelta, timezone

from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.db.models import ForceOverride
from hall_monitor.discord_bot.permissions import is_janitor
from hall_monitor.services.time_parse import InvalidDuration, parse as parse_duration

_JANITOR_MAX = timedelta(days=90)


def gating_rejection(delta: timedelta | None, is_monitor: bool) -> str | None:
    """Pure permissions check for the parsed duration.

    Returns a user-facing rejection message when the caller isn't allowed to
    use this duration, or ``None`` when the request should proceed.
    Extracted from the command handler so it's unit-testable without
    faking a Discord context.
    """
    if is_monitor:
        return None
    if delta is None:
        return "permanent overrides are monitor-only; try `3mo` or shorter."
    if delta > _JANITOR_MAX:
        return (
            "janitor overrides can't run past three months (`3mo`); "
            "ask a monitor for anything longer."
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
