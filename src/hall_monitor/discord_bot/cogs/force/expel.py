"""``~force expel`` — bar a guild from the Hall without a delegate vote.

Monitor-only, and it reaches exactly the same end state as a motion that
carries (``services/expel.py``): the ban row, the representatives
removed, the contact slots cleared. The difference is that it's quiet —
no post, no ballot, nothing for anybody to argue with in a channel.

That quietness is the point and the risk. It exists for the cases a vote
can't sensibly answer — a guild that has to go now, or one that never
had a seat and so could never be moved against — and it should not
become the ordinary way guilds leave. It logs loudly for that reason.

``~unforce expel`` deletes the ban and **nothing else**. Nobody is
invited back and no role is restored: their ``Delegate`` rows are marked
left and their slots are gone, so returning means verifying again, which
is what returning means for anyone else who left. The plan called this
"restores the guild"; there is no such state for it to be restored to,
and inventing one would be inventing something the rest of the Hall
can't reach.
"""

import logging

from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_monitor
from hall_monitor.services import expel, expel_motion

logger = logging.getLogger(__name__)


async def bar(ctx: commands.Context, guild_tag: str) -> str:
    """Do the expulsion and return what to say about it.

    Split out from the command so the behaviour is testable without a
    Discord context, the same shape as ``force/guild.apply_now``.
    """
    if ctx.guild is None:
        return "run this in the server — it removes members."
    if await expel.is_banned(guild_tag):
        return f"`{guild_tag}` is already barred from the Hall."

    logger.warning(
        "expel: %s (%s) is barring %s by hand, with no vote",
        ctx.author,
        getattr(ctx.author, "id", None),
        guild_tag,
    )
    removal = await expel.expel(
        ctx.guild,
        guild_tag,
        reason=f"hall-monitor: ~force expel by {ctx.author}",
        by_discord_user_id=getattr(ctx.author, "id", None),
    )
    # An open motion against them is no longer a question anybody can
    # answer, and its buttons would still work.
    superseded = await expel_motion.supersede_open(ctx.guild, guild_tag)
    for motion in superseded:
        await _refresh(ctx, motion)
    return _report(guild_tag, removal, len(superseded))


async def unbar(ctx: commands.Context, guild_tag: str) -> str:
    if not await expel.lift(guild_tag):
        return f"`{guild_tag}` isn't barred — nothing to lift."
    logger.warning(
        "expel: %s (%s) lifted the ban on %s",
        ctx.author,
        getattr(ctx.author, "id", None),
        guild_tag,
    )
    return (
        f"`{guild_tag}` is no longer barred. Nobody has been invited back and "
        "no roles were restored — their representatives verify again from "
        "scratch, the same as anyone else who left."
    )


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="expel")
    @is_monitor()
    async def force_expel(ctx: commands.Context, guild_tag: str) -> None:
        """bar a guild from the Hall without putting it to a vote"""
        await ctx.reply(await bar(ctx, guild_tag))

    @cog.unforce.command(name="expel")
    @is_monitor()
    async def unforce_expel(ctx: commands.Context, guild_tag: str) -> None:
        """let a barred guild back into the Hall"""
        await ctx.reply(await unbar(ctx, guild_tag))


def _report(guild_tag: str, removal: expel.Removal, superseded: int) -> str:
    """Say what actually happened, including when that was nothing.

    A `~force expel` against a guild with no presence here is a perfectly
    ordinary thing to do — barring one before it ever arrives — and it
    has to read as success rather than as a command that didn't work.
    """
    parts = [f"`{guild_tag}` is barred from the Hall."]
    if removal.kicked:
        parts.append(f"Removed {len(removal.kicked)} representative(s).")
    if removal.slots_cleared:
        parts.append(f"Cleared {removal.slots_cleared} contact slot(s).")
    if not removal.kicked and not removal.slots_cleared:
        parts.append("It had nobody here, so nobody was removed.")
    if removal.failed:
        parts.append(
            f"**{len(removal.failed)} couldn't be kicked** — check my role "
            "position; the hourly sweep will try again."
        )
    if superseded:
        parts.append(f"Closed {superseded} open motion(s) against them.")
    return " ".join(parts)


async def _refresh(ctx: commands.Context, motion) -> None:
    # Imported here rather than at module scope: the moderation cog
    # imports `roster`, which imports `expel`, and this module is loaded
    # from `force/__init__` during that same walk.
    from hall_monitor.discord_bot.cogs.moderation import expel as expel_cog

    await expel_cog.refresh(ctx.bot, motion)
