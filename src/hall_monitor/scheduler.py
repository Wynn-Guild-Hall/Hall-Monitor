"""Background jobs — notability refresh and pending-invite expiry sweep."""

import functools

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.services import discord_invites, notability


def build_scheduler(bot: commands.Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        notability.refresh_all,
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
