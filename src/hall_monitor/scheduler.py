"""Background jobs — notability refresh and pending-invite expiry sweep."""

import functools
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.discord_bot.cogs.moderation import expel as expel_cog
from hall_monitor.services import (
    delegate_registry,
    discord_invites,
    emote_slots,
    expel,
    expel_motion,
    notability,
    roster,
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

    One job rather than four intervals, in this order deliberately: the
    reconcile reads both the notability cache and each delegate's current
    guild, so gathering them separately would spend an hour of every
    change acting on the previous sweep's numbers. The roster goes last
    for the same reason — it publishes what the pass just settled.

    The roster sync here is also the backstop for the event-driven
    `roster.request_sync` calls: a hook someone forgets to add, or a pass
    that lost a race with Discord, costs at most an hour of staleness
    rather than permanent drift.
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

    # Before the roster, so an expulsion is reflected in the same pass it
    # happens. Open motions are settled here as well as on each vote,
    # because the electorate moves on its own: guilds gain and lose their
    # seat while a vote is open, and a motion can reach the bar with no
    # new vote behind it (DESIGN.md §16).
    await _settle_expulsions(bot, guild)

    try:
        published = await roster.sync_channel(guild)
    except Exception:  # noqa: BLE001 — a channel edit must not cost the sweep
        logger.exception("roster: sync failed")
        return
    if published is not None:
        logger.info("roster: %s", published.line())

    # Last: the banners the roster's top entries wear. It runs after the
    # roster rather than before because it takes its top-N from the same
    # ordering the roster just published — minting for a list that's
    # about to change would spend an upload on a guild already falling
    # out of the budget.
    try:
        emotes = await emote_slots.reconcile(guild)
    except Exception:  # noqa: BLE001 — decoration must not cost the sweep
        logger.exception("emotes: reconcile failed")
        return
    logger.info("emotes: %s", emotes.line())

    # An emote that moved took a new ID with it — Discord has no
    # replace-in-place — so the roster messages written moments ago now
    # point at emotes that no longer exist. Redraw. The request is
    # debounced, so a pass that changed nothing costs nothing.
    if emotes.minted or emotes.refreshed or emotes.evicted:
        roster.request_sync(guild)


async def _settle_expulsions(bot: commands.Bot, guild) -> None:
    """Resolve open motions, then re-remove anyone a ban should have caught.

    Both halves are best-effort: an expulsion is rare and a failure here
    must not cost the roster and emote passes that follow. `enforce` runs
    second so that a motion carrying this minute is already reflected in
    what it checks.
    """
    try:
        for resolution in await expel_motion.resolve_open(guild):
            await expel_cog.refresh(bot, resolution.motion)
    except Exception:  # noqa: BLE001 — a vote must not cost the sweep
        logger.exception("expel: couldn't resolve open motions")

    try:
        await expel.enforce(guild)
    except Exception:  # noqa: BLE001
        logger.exception("expel: the ban sweep failed")
