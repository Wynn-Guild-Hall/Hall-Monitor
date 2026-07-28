"""``~force rep`` — point a representative at a different guild for good.

The manual fix for the gap Stage 9 left (DESIGN.md §12.2). A rep who
moves guilds correctly becomes an External Relegate, but if they then
become a chief of their *new* guild there is no way for them to
represent it: ``mint_invite`` refuses while their ``Delegate`` row is
live, and releasing the row doesn't help either, because clicking an
invite as an **existing member fires no** ``on_member_join``, so the
``PendingInvite`` is never consumed. Short of asking them to leave the
server, only a direct edit fixes it.

**Not the same command as ``~force guild``.** That one asserts who
somebody speaks for *temporarily*, as an override sitting in front of
their row; this rewrites the row itself. `~force guild` expires and hands
them back; this doesn't, because it isn't a correction — it's the
paperwork for a change that really happened.

**It checks Wynncraft rather than trusting the operator.** This is the
one place a human asserts something the game is otherwise the authority
on, so the command verifies chief-hood of the target guild itself and
refuses otherwise. A monitor who is certain and wrong here produces a
representative the reconcile can't explain and nobody can undo without
knowing what the row used to say.
"""

import logging

import discord
from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_monitor
from hall_monitor.services import (
    contacts,
    delegate_registry,
    expel,
    guild_tag as tags,
    transitions,
)

logger = logging.getLogger(__name__)


async def repoint(
    ctx: commands.Context, user: discord.Member, guild_tag: str
) -> str:
    """Re-point ``user`` at ``guild_tag`` and return what to say about it.

    Split out from the command so the behaviour is testable without a
    Discord context, the same shape as ``force/guild.apply_now``.
    """
    if ctx.guild is None:
        return "run this in the server — it moves roles about."

    delegate = await delegate_registry.get_by_discord_user_id(user.id)
    if delegate is None:
        return (
            f"{user.mention} isn't a registered representative, so there's "
            "nothing to re-point. They verify through the join flow."
        )
    if await expel.is_banned(guild_tag):
        return f"`{guild_tag}` is barred from the Hall."
    if tags.matches(delegate.guild_tag, guild_tag):
        return (
            f"{user.mention} already represents `{delegate.guild_tag}` on their "
            "row — nothing to change."
        )

    # Wynncraft, not the operator. Verified before anything is written, so
    # a refusal leaves the member exactly as they were.
    confirmed = await delegate_registry.eligible_guild(delegate.mc_uuid)
    if confirmed is None or not tags.matches(confirmed.prefix, guild_tag):
        return _refusal(user, guild_tag, confirmed)

    was = delegate.guild_tag
    # Slots for the old guild go, and go *without* a kick: they aren't
    # leaving and nobody took anything off them (`contacts.vacate_holdings`).
    given_up = await contacts.vacate_holdings(delegate, was, discord_guild=ctx.guild)
    # A live `~force guild` sits in front of the row, so leaving one in
    # place would make this command appear to have done nothing at all.
    dropped_override = bool(await delegate_registry.clear_forced_guild(user.id))

    delegate.guild_tag = confirmed.prefix
    delegate.current_guild_tag = confirmed.prefix
    await delegate.save(update_fields=["guild_tag", "current_guild_tag"])

    logger.warning(
        "rep: %s (%s) re-pointed %s from %s to %s (gave up %s)",
        ctx.author,
        getattr(ctx.author, "id", None),
        user.id,
        was,
        confirmed.prefix,
        ", ".join(given_up) or "no slots",
    )

    # Settled now and reported, rather than left to the hourly pass — a
    # command whose effect only shows up an hour later is
    # indistinguishable from one that silently did nothing, which is how
    # four bugs in a row got to production (DESIGN.md §12.3).
    settlement = await transitions.settle_representative(ctx.guild, delegate)
    return _report(
        user, was, confirmed.prefix, given_up, dropped_override, settlement
    )


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="rep")
    @is_monitor()
    async def force_rep(
        ctx: commands.Context, user: discord.Member, guild_tag: str
    ) -> None:
        """re-point a representative at the guild they've actually joined"""
        await ctx.reply(await repoint(ctx, user, guild_tag))


def _refusal(user: discord.Member, guild_tag: str, confirmed) -> str:
    if confirmed is None:
        return (
            f"Wynncraft doesn't have {user.mention} as a chief or owner of any "
            f"guild, so I can't make them `{guild_tag}`'s representative. If "
            "they've just been promoted, it can take a few minutes to show."
        )
    return (
        f"Wynncraft has {user.mention} as a chief of `{confirmed.prefix}`, not "
        f"`{guild_tag}`. Re-run it with `{confirmed.prefix}` if that's the one "
        "you meant."
    )


def _report(
    user: discord.Member,
    was: str,
    now: str,
    given_up: list[str],
    dropped_override: bool,
    settlement,
) -> str:
    parts = [
        f"{user.mention} now represents `{now}` (was `{was}`), confirmed "
        "against Wynncraft."
    ]
    if given_up:
        parts.append(
            "They gave up `" + "`, `".join(given_up) + f"` for `{was}` — no kick, "
            "they're staying."
        )
    if dropped_override:
        parts.append("Dropped their `~force guild` override, which outranked this.")
    if settlement is None:
        parts.append("They aren't in the server, so nothing was applied.")
    else:
        parts.append(f"Now {settlement.line()}.")
    return " ".join(parts)
