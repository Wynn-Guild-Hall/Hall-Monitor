"""Background jobs — notability refresh and pending-invite expiry sweep."""

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
    scheduler.add_job(
        discord_invites.sweep_expired,
        "interval",
        seconds=settings.pending_invite_sweep_seconds,
        id="pending-invite-sweep",
    )
    return scheduler
