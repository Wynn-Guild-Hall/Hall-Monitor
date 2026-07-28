"""Expulsion — a guild barred from the Hall, and its representatives removed.

Two ways in: a delegate motion that passes its vote
(``services/expel_motion.py``), or a monitor's ``~force expel``. Both land
here, because they mean the same thing and should leave the server in the
same state.

**A ban is one row.** ``ExpelBan`` exists or it doesn't; there's no
suspended, pending or partially-expelled state. Lifting a ban is a delete,
and the guild's former representatives verify again from scratch — which
is the honest reading, since while the ban stood they held nothing.

The ban bites in five places, and it has to be all five or a removed
guild finds its own way back in:

- **The `/join` lookup.** The Hallway page is where a chief starts, so a
  ban that only bit later would walk them through picking roles and
  generating a code before anything told them (``sidecar/routes/join.py``).
- **The verify route.** A chief of a banned guild is answered in chat and
  no invite is minted (``sidecar/routes/verify.py``).
- **The join listener.** An invite minted *before* the ban is still live
  for its ten minutes, so the redemption is checked too and the member is
  removed on arrival (``cogs/listeners/on_join.py``).
- **The roster.** A banned guild is not listed, whatever its notability
  says — expulsion is about welcome, not about significance, and a guild
  can be both notable and unwelcome (``services/roster.py``).
- **The hourly sweep**, via :func:`enforce`. That's the backstop for the
  other four and for everything nobody thought of: a ``~force guild``
  pointed at a banned tag, a member who slipped in while the bot was
  down, a kick that 403'd the first time. Same reconcile shape as §12 and
  §14 — it reads the current state and makes the server match, so no
  event can be missed.

Nothing here deletes a ``Delegate`` row. It's marked left, like any other
departure, so the history survives and a lifted ban leaves a legible
record of who used to be here.
"""

import logging
from dataclasses import dataclass, field

import discord

from hall_monitor.db.models import Delegate, ExpelBan, ExpelMotion, GuildContact
from hall_monitor.services import (
    delegate_registry,
    guild_tag as tags,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Removal:
    """What removing one guild's representatives actually did."""

    guild_tag: str
    kicked: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    slots_cleared: int = 0

    @property
    def touched(self) -> int:
        return len(self.kicked) + len(self.failed)

    def line(self) -> str:
        parts = [f"{len(self.kicked)} representative(s) removed"]
        if self.slots_cleared:
            parts.append(f"{self.slots_cleared} contact slot(s) cleared")
        if self.failed:
            parts.append(f"{len(self.failed)} couldn't be kicked")
        return f"`{self.guild_tag}`: " + ", ".join(parts)


# --------------------------------------------------------------------------
# The ban itself
# --------------------------------------------------------------------------


async def is_banned(guild_tag: str) -> bool:
    """Whether ``guild_tag`` is currently barred from the Hall."""
    return await ExpelBan.filter(guild_tag__iexact=guild_tag).exists()


async def banned_tags() -> set[str]:
    """Comparison keys of every banned guild, in one query.

    The bulk counterpart to :func:`is_banned`, for callers weighing up
    every guild at once — the roster asking per guild would be a query
    each for an answer one query gives for all of them.
    """
    return {
        tags.normalise(row["guild_tag"])
        for row in await ExpelBan.all().values("guild_tag")
    }


async def record_ban(
    guild_tag: str,
    *,
    reason: str,
    motion: ExpelMotion | None = None,
    by_discord_user_id: int | None = None,
) -> ExpelBan:
    """Write the ban row, matching an existing one case-insensitively.

    Re-banning an already-banned guild updates the reason rather than
    creating a second row: ``VETS`` and ``vets`` are one guild, and two
    rows would mean a single ``~unforce expel`` left one of them standing.
    """
    existing = await ExpelBan.filter(guild_tag__iexact=guild_tag).first()
    if existing is not None:
        existing.reason = reason
        existing.motion = motion
        existing.created_by_discord_user_id = by_discord_user_id
        await existing.save()
        return existing
    return await ExpelBan.create(
        guild_tag=guild_tag,
        reason=reason,
        motion=motion,
        created_by_discord_user_id=by_discord_user_id,
    )


async def lift(guild_tag: str) -> bool:
    """Drop the ban. Returns whether there was one to drop.

    Deliberately *only* the ban: nobody is invited back and no role is
    restored. Their ``Delegate`` rows are marked left and their slots are
    gone, so returning means verifying again — which is what it means for
    anyone else who left, and inventing a restore path here would be
    inventing a state the rest of the Hall has no way to reach.
    """
    return bool(await ExpelBan.filter(guild_tag__iexact=guild_tag).delete())


# --------------------------------------------------------------------------
# Removal
# --------------------------------------------------------------------------


async def expel(
    discord_guild: discord.Guild | None,
    guild_tag: str,
    *,
    reason: str,
    motion: ExpelMotion | None = None,
    by_discord_user_id: int | None = None,
) -> Removal:
    """Bar a guild and remove everyone speaking for it.

    The ban is written **first**. A removal that dies partway through
    still leaves the guild barred, so nothing it missed can re-verify
    while somebody works out what happened; the hourly :func:`enforce`
    then finishes the job. The other order would leave a window in which
    the reps were gone and the door was open.
    """
    await record_ban(
        guild_tag,
        reason=reason,
        motion=motion,
        by_discord_user_id=by_discord_user_id,
    )
    removal = await remove_representatives(discord_guild, guild_tag, reason=reason)
    logger.info("expel: %s (%s)", removal.line(), reason)
    return removal


async def remove_representatives(
    discord_guild: discord.Guild | None, guild_tag: str, *, reason: str
) -> Removal:
    """Kick everyone who speaks for ``guild_tag`` and vacate its slots.

    Membership is by who *represents* the tag (``~force guild`` can
    repoint that), which is the same rule every other pass uses — settling
    by the row instead would miss a repointed member and catch one who has
    moved on.

    A kick that fails is logged and counted, not raised. The ban is what
    keeps the guild out; a representative still sitting in the server is
    visible and fixable, and the next :func:`enforce` tries again.
    """
    slots = await GuildContact.filter(guild_tag__iexact=guild_tag).delete()
    removal = Removal(guild_tag=guild_tag, slots_cleared=slots)

    for delegate in await Delegate.filter(left_at=None):
        represented = await delegate_registry.represented_guild(delegate)
        if not tags.matches(represented, guild_tag):
            continue
        # Marked left whatever Discord does — the row is what the guild
        # watch, the reconcile and `mint_invite` all read, and a row that
        # still looks live would have us polling Wynncraft hourly for
        # somebody who has been removed.
        await delegate_registry.mark_left(delegate.discord_user_id)
        member = (
            discord_guild.get_member(delegate.discord_user_id)
            if discord_guild is not None
            else None
        )
        if member is None:
            continue
        try:
            await member.kick(reason=reason)
        except discord.HTTPException:
            logger.exception(
                "expel: couldn't kick %s after %s was expelled",
                delegate.discord_user_id,
                guild_tag,
            )
            removal.failed.append(delegate.discord_user_id)
            continue
        removal.kicked.append(delegate.discord_user_id)
    return removal


async def enforce(discord_guild: discord.Guild) -> list[Removal]:
    """Re-remove anyone speaking for a banned guild. Hourly backstop.

    Normally finds nothing: :func:`expel` cleared the guild when the ban
    was written. It's here for the cases that aren't the normal one — a
    kick that came back 403, a ``~force guild`` pointed at a banned tag, a
    member who joined on a stale invite while the bot was down. Every one
    of those is a guild the Hall voted out quietly walking back in, and
    none of them is an event we could have hooked.
    """
    removals = []
    for tag in await ExpelBan.all().values_list("guild_tag", flat=True):
        removal = await remove_representatives(
            discord_guild,
            tag,
            reason=f"hall-monitor: {tag} is expelled from the Guild Hall",
        )
        if removal.touched or removal.slots_cleared:
            logger.warning("expel: %s (found by the hourly sweep)", removal.line())
            removals.append(removal)
    return removals
