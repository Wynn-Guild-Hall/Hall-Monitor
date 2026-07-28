"""Background jobs — notability refresh and pending-invite expiry sweep."""

import functools
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.services import (
    delegate_registry,
    discord_invites,
    notability,
    transitions,
)

logger = logging.getLogger(__name__)


def build_scheduler(bot: commands.Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        functools.partial(refresh_and_reconcile, bot),
        "interval",
        seconds=settings.notability_refresh_seconds,
        id="notability-refresh",
    )
    # Sweep needs the bot to revoke stale Discord invites. Bind it here so
    # discord_invites stays a plain service module with no hidden globals.
    scheduler.add_job(
        functools.partial(discord_invites.sweep_expired, bot=bot),
        "interval",
        seconds=settings.pending_invite_sweep_seconds,
        id="pending-invite-sweep",
    )
    return scheduler


async def refresh_and_reconcile(bot: commands.Bot) -> None:
    """Re-gather the facts, then make Discord match them.

    One job rather than three intervals, in this order deliberately: the
    reconcile reads both the notability cache and each delegate's current
    guild, so gathering them separately would spend an hour of every
    change acting on the previous sweep's numbers.
    """
    await notability.refresh_all()
    checked, external = await delegate_registry.refresh_current_guilds()
    logger.info(
        "guild watch: %d delegate(s) checked, %d representing a guild they've left",
        checked,
        external,
    )

    guild = (
        bot.get_guild(settings.discord_guild_id) if settings.discord_guild_id else None
    )
    if guild is None:
        logger.warning(
            "reconcile skipped: guild %s unavailable", settings.discord_guild_id
        )
        return
    summary = await transitions.reconcile(guild)
    logger.info("reconcile: %s", summary.line())
