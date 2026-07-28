"""``~script delegate_status`` — is the guild watch actually working?

The watch keeps a delegate's last known guild when a lookup fails, which
is right per-call: a 429 recorded as "no guild" would read back as them
having *rejoined*, quietly promoting somebody the last sweep relegated.
Across calls it's unbounded, though — sustained 429s or a payload shape
change would freeze every delegate's guild and nothing would say so. The
brief's guarantee is that a guild change is noticed within 48h, and this
is what turns "eventually" into a number somebody can look at.

Read-only. It reports what the watch has managed; `~script guild_watch`
runs the poll.
"""

from datetime import datetime, timezone

from hall_monitor.db.models import Delegate
from hall_monitor.services import delegate_registry


async def main(ctx, *args: str) -> None:
    live = await Delegate.filter(left_at=None)
    if not live:
        await ctx.reply("no live representatives on file.")
        return

    stale = await delegate_registry.stale_delegates()
    hours = int(delegate_registry.STALE_AFTER.total_seconds() // 3600)
    never = [one for one in live if one.current_guild_checked_at is None]

    lines = [
        f"**Guild watch** — {len(live)} live representative(s)",
        f"· checked inside {hours}h: **{len(live) - len(stale)}**",
        f"· overdue: **{len(stale)}**" + ("" if stale else " — nothing to chase"),
    ]
    if never:
        lines.append(
            f"· never successfully checked: **{len(never)}** (a row written in "
            "the last hour simply hasn't had its first sweep)"
        )

    for delegate in sorted(stale, key=_age, reverse=True)[:20]:
        seen = (
            f"<t:{int(delegate.current_guild_checked_at.timestamp())}:R>"
            if delegate.current_guild_checked_at
            else "never"
        )
        lines.append(
            f"  · <@{delegate.discord_user_id}> (`{delegate.mc_username or delegate.mc_uuid}`) "
            f"— represents `{delegate.guild_tag}`, last checked {seen}"
        )
    if len(stale) > 20:
        lines.append(f"  · …and {len(stale) - 20} more")

    if stale:
        lines += [
            "",
            "All of them overdue at once usually means the poll itself is "
            "failing rather than any one account — check the log for "
            "`guild watch: couldn't look up`.",
        ]
    await ctx.reply("\n".join(lines))


def _age(delegate: Delegate) -> float:
    """Seconds since a successful check. Never-checked sorts oldest."""
    if delegate.current_guild_checked_at is None:
        return float("inf")
    return (
        datetime.now(timezone.utc) - delegate.current_guild_checked_at
    ).total_seconds()
