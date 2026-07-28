"""Per-role contact assignment with uniqueness enforcement.

One contact per role per guild. Claiming a slot displaces whoever held
it: they lose the Discord role, and if that was their last contact role
they're kicked from the server — the Hall is a room of guild
representatives, and a delegate representing nothing has no seat at the
table.

Discord is reached through a ``discord.Guild`` handle the caller passes
in, so everything here stays testable with plain mocks and degrades to
DB-only bookkeeping when the bot isn't connected. Discord-side failures
are logged rather than raised: the database is the roster's source of
truth, and a stale role left on a displaced holder is visible and fixable
in a way that a slot which silently refused to move is not.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from hall_monitor.config import settings
from hall_monitor.db.models import Delegate, GuildContact
from hall_monitor.services import delegate_registry, guild_tag as tags

logger = logging.getLogger(__name__)

CONTACT_ROLES = ("events", "housing", "warring", "ownership")

_CONTACT_ROLE_SETTINGS = {
    "events": "events_contact_role_id",
    "housing": "housing_contact_role_id",
    "warring": "warring_contact_role_id",
    "ownership": "ownership_contact_role_id",
}


class UnknownContactRole(KeyError):
    """Raised for a role name outside :data:`CONTACT_ROLES`."""


class NotTheirGuild(Exception):
    """Raised when a slot is claimed for someone who doesn't speak for it.

    Two ways to get here, and the caller wants to say which: they
    represent a *different* guild (``represents``), or they represent this
    one but have drifted off to another (``playing_for``).
    """

    def __init__(
        self,
        delegate: "Delegate",
        *,
        represents: str,
        playing_for: str | None = None,
    ) -> None:
        super().__init__(represents)
        self.delegate = delegate
        self.represents = represents
        self.playing_for = playing_for


@dataclass(frozen=True)
class Displacement:
    """A delegate who lost a contact role, and what it cost them."""

    delegate: Delegate
    role: str
    kicked: bool


def contact_role_id(role: str) -> int:
    """Discord role ID configured for a contact role name.

    Reads ``settings`` per call rather than caching at import time — the
    IDs are deploy-time config, and tests monkeypatch them.
    """
    try:
        attribute = _CONTACT_ROLE_SETTINGS[role]
    except KeyError as exc:
        raise UnknownContactRole(role) from exc
    return getattr(settings, attribute)


async def represents(delegate: Delegate, guild_tag: str) -> bool:
    """Whether this delegate currently speaks for ``guild_tag``.

    Both halves matter: the guild has to be the one they represent (which
    ``~force guild`` can repoint), and they can't have drifted off to
    somewhere else on their own.

    This is the condition a contact slot is *kept* on: the reconcile
    vacates any slot whose holder fails it (:func:`sync_contact_roles`).
    Callers that display a guild's contacts — the roster, chiefly —
    should still ask, because there are seconds between somebody changing
    sides and the pass that clears the row, and naming a contact who has
    left points the room at the wrong person.
    """
    represented = await delegate_registry.represented_guild(delegate)
    if not tags.matches(represented, guild_tag):
        return False
    return not await delegate_registry.is_external(delegate)


async def current_contacts_for_guild(
    guild_tag: str, discord_guild: discord.Guild | None = None
) -> dict[str, Delegate | None]:
    """Which live delegate currently holds each contact role for ``guild_tag``.

    Every role in :data:`CONTACT_ROLES` is present as a key. Value is the
    :class:`Delegate` row when the slot is claimed by someone still in the
    Discord server, else ``None``. When ``discord_guild`` is provided we
    also drop rows whose delegate has left mid-session (belt-and-braces
    against the DB not having caught up to the leave listener yet).
    """
    result: dict[str, Delegate | None] = {role: None for role in CONTACT_ROLES}
    rows = await GuildContact.filter(
        guild_tag__iexact=guild_tag
    ).prefetch_related("delegate")
    for row in rows:
        if row.role not in result:
            continue  # legacy role no longer in CONTACT_ROLES — ignore
        delegate = row.delegate
        still_here = await delegate_registry.is_current_member(
            delegate.mc_uuid, discord_guild=discord_guild
        )
        if still_here:
            result[row.role] = delegate
    return result


async def current_holder(guild_tag: str, role: str) -> Delegate | None:
    """The delegate the DB has holding ``role`` for ``guild_tag``.

    Deliberately *not* cross-checked against Discord: this is the row
    :func:`assign_contact` has to move, and a holder who has wandered off
    still owns the slot until something clears it. Use
    :func:`current_contacts_for_guild` for the display side.
    """
    row = await _slot(guild_tag, role)
    return row.delegate if row is not None else None


async def assign_contact(
    guild_tag: str,
    role: str,
    delegate: Delegate,
    *,
    discord_guild: discord.Guild | None = None,
    grant: bool = True,
    reason: str | None = None,
) -> Displacement | None:
    """Make ``delegate`` the ``role`` contact for ``guild_tag``.

    Returns the prior holder's :class:`Displacement` when there was one,
    else ``None`` — a free slot and a re-assignment to the delegate who
    already holds it both displace nobody.

    ``grant=False`` is for callers that have already applied the role
    themselves: the join listener adds a member's whole role set in one
    request, and re-adding it here would just spend rate limit. The DB
    move and the displacement happen either way.

    Raises :class:`NotTheirGuild` unless they actually speak for
    ``guild_tag`` — the reconcile grants a contact role on exactly that
    condition (see :func:`sync_contact_roles`), so claiming a slot for
    anyone else would look like it worked and be stripped within the hour.
    """
    _require_known(role)
    await _require_represents(delegate, guild_tag)
    reason = reason or f"hall-monitor: {guild_tag} {role} contact reassigned"

    row = await _slot(guild_tag, role)
    prior = row.delegate if row is not None else None
    if prior is not None and prior.id == delegate.id:
        return None

    if row is None:
        # Store the spelling we were handed; an existing row keeps its own,
        # since lookups fold case and two rows differing only in case would
        # break the one-contact-per-role invariant the DB can't see.
        await GuildContact.create(guild_tag=guild_tag, role=role, delegate=delegate)
    else:
        row.delegate = delegate
        row.assigned_at = datetime.now(timezone.utc)
        await row.save()

    if grant:
        await _grant_role(delegate, role, discord_guild=discord_guild, reason=reason)
    if prior is None:
        return None
    return await _release(prior, role, discord_guild=discord_guild, reason=reason)


async def clear_contact(
    guild_tag: str,
    role: str,
    *,
    delegate: Delegate | None = None,
    discord_guild: discord.Guild | None = None,
    reason: str | None = None,
) -> Displacement | None:
    """Vacate ``guild_tag``'s ``role`` slot, leaving it unclaimed.

    With ``delegate`` given the slot is only cleared if it's theirs, so
    ``~unforce assign @someone events`` can't quietly evict whoever
    actually holds the role. Returns ``None`` when there was nothing to
    clear.
    """
    _require_known(role)
    row = await _slot(guild_tag, role)
    if row is None:
        return None
    holder = row.delegate
    if delegate is not None and holder.id != delegate.id:
        return None

    await row.delete()
    return await _release(
        holder,
        role,
        discord_guild=discord_guild,
        reason=reason or f"hall-monitor: {guild_tag} {role} contact cleared",
    )


async def sync_contact_roles(
    guild_tag: str,
    *,
    discord_guild: discord.Guild | None,
    granted: bool,
    reason: str | None = None,
) -> int:
    """Hand out or take back the Discord contact roles for one guild's slots.

    Two different situations, resolved differently on purpose.

    **The guild isn't major.** It has dropped out of the Hall for now,
    so its contact roles are withdrawn and handed back — to the same
    people — when it returns. The ``GuildContact`` rows are left exactly
    as they are: major-guild status decides who may *use* a contact role, the row
    decides who holds the slot, and clearing them would cost a returning
    guild four re-verifications and lose the record of who to hand the
    roles back to. Nobody is kicked for it either — the kick is what
    happens when you lose your slot to someone else, not when your guild
    has a quiet quarter.

    **The holder no longer speaks for this guild**, having drifted
    elsewhere or been repointed by ``~force guild``. Then the slot is
    *vacated* — see :func:`_vacate`. Not withheld: a slot somebody holds
    and cannot use is two answers to one question, and every screen that
    shows it has to pick one.

    Returns the number of slots that actually changed, so a reconcile with
    nothing to do says so.
    """
    reason = reason or (
        f"hall-monitor: {guild_tag} is major"
        if granted
        else f"hall-monitor: {guild_tag} is no longer major"
    )

    changed = 0
    rows = await GuildContact.filter(guild_tag__iexact=guild_tag).prefetch_related(
        "delegate"
    )
    for row in rows:
        if row.role not in _CONTACT_ROLE_SETTINGS:
            continue
        # Checked before anything Discord-side, because the row going is
        # the point — a slot held by somebody who has stopped speaking for
        # this guild is vacant, and should read that way everywhere.
        if not await represents(row.delegate, guild_tag):
            changed += await _vacate(row, discord_guild=discord_guild)
            continue
        if discord_guild is None:
            continue  # rows are settled; there's no Discord side to settle
        member = _member_for(row.delegate, discord_guild)
        if member is None:
            continue
        discord_role = _discord_role(discord_guild, row.role)
        if discord_role is None:
            continue
        wanted = granted
        holds = any(existing.id == discord_role.id for existing in member.roles)
        if holds == wanted:
            continue  # already correct — an hourly pass must be quiet
        if wanted:
            await _add_role(member, discord_role, role=row.role, reason=reason)
        else:
            await _remove_role(member, row.role, reason=reason)
        changed += 1
    return changed


async def vacate_holdings(
    delegate: Delegate,
    guild_tag: str,
    *,
    discord_guild: discord.Guild | None = None,
) -> list[str]:
    """Give up every slot ``delegate`` holds for ``guild_tag``. No kick.

    For when somebody stops speaking for a guild while staying in the
    Hall — ``~force rep`` re-pointing them at a new one, and the same
    situation the reconcile handles by itself (:func:`sync_contact_roles`).
    The kick belongs to :func:`_release`, which is about losing a slot to
    *somebody else*; here nobody has taken anything and they aren't
    leaving.

    Returns the role names given up, so the caller can say what it did
    rather than leaving the member to notice.
    """
    rows = await GuildContact.filter(
        guild_tag__iexact=guild_tag, delegate_id=delegate.id
    )
    given_up = []
    for row in rows:
        row.delegate = delegate  # already loaded; skip the prefetch
        await _vacate(row, discord_guild=discord_guild)
        given_up.append(row.role)
    return sorted(given_up)


async def resolve_conflicts_and_kick_if_empty(
    guild_tag: str,
    roles: set[str],
    new_holder: Delegate,
    *,
    discord_guild: discord.Guild | None = None,
    grant: bool = True,
    reason: str | None = None,
) -> list[Displacement]:
    """Claim several of a guild's slots for one delegate at once.

    Roles are taken in a stable order so that a prior holder losing more
    than one of them is only kicked on the last — the remaining-roles
    count is re-read per role.
    """
    displaced = []
    for role in sorted(roles):
        one = await assign_contact(
            guild_tag,
            role,
            new_holder,
            discord_guild=discord_guild,
            grant=grant,
            reason=reason,
        )
        if one is not None:
            displaced.append(one)
    return displaced


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _require_known(role: str) -> None:
    if role not in _CONTACT_ROLE_SETTINGS:
        raise UnknownContactRole(role)


async def _require_represents(delegate: Delegate, guild_tag: str) -> None:
    if await represents(delegate, guild_tag):
        return
    raise NotTheirGuild(
        delegate,
        represents=await delegate_registry.represented_guild(delegate),
        playing_for=delegate.current_guild_tag,
    )


async def _vacate(row: GuildContact, *, discord_guild: discord.Guild | None) -> int:
    """Give a slot up entirely, because its holder no longer speaks for it.

    The alternative — keeping the row and withholding the role — makes a
    slot that is simultaneously held and unclaimed, and every screen then
    has to pick an answer: the roster said unclaimed, the member list said
    they didn't have the role, and ``~script standing`` said the slot was
    theirs. Whoever reads two of those reasonably concludes one is broken.

    So the row goes. Note what this deliberately isn't:

    - **Not a kick.** The kick is for losing a slot to somebody else
      (:func:`_release`). Moving guilds already costs a representative
      their standing and their colour; it doesn't cost them the room.
    - **Not the non-major path.** A dormant guild keeps its rows, and
      the same four people get their roles back when it returns — nobody
      there has stopped being who they were.

    Coming back re-earns the slot rather than restoring it, which is the
    honest reading: while they were gone the slot was free for anyone.
    """
    delegate = row.delegate
    await row.delete()
    member = _member_for(delegate, discord_guild)
    if member is not None:
        await _remove_role(
            member,
            row.role,
            reason=f"hall-monitor: {row.guild_tag} {row.role} vacated — "
            "the holder no longer speaks for that guild",
        )
    logger.info(
        "contacts: vacated %s's %s slot — %s doesn't speak for them any more",
        row.guild_tag,
        row.role,
        delegate.discord_user_id,
    )
    return 1


async def _slot(guild_tag: str, role: str) -> GuildContact | None:
    """The ``(guild_tag, role)`` row, matched case-insensitively on the tag."""
    return (
        await GuildContact.filter(guild_tag__iexact=guild_tag, role=role)
        .prefetch_related("delegate")
        .first()
    )


async def _release(
    delegate: Delegate,
    role: str,
    *,
    discord_guild: discord.Guild | None,
    reason: str,
) -> Displacement:
    """Strip ``role`` from a former holder, kicking them if it was their last.

    Called *after* the row has moved, so the remaining-roles count below
    reads the post-change state.
    """
    member = _member_for(delegate, discord_guild)
    if member is not None:
        await _remove_role(member, role, reason=reason)

    remaining = await GuildContact.filter(delegate_id=delegate.id).count()
    if remaining:
        logger.info(
            "contacts: %s lost the %s slot, still holds %d contact role(s)",
            delegate.discord_user_id,
            role,
            remaining,
        )
        return Displacement(delegate=delegate, role=role, kicked=False)
    return Displacement(
        delegate=delegate,
        role=role,
        kicked=await _kick(delegate, member, reason=reason),
    )


async def _kick(
    delegate: Delegate, member: discord.Member | None, *, reason: str
) -> bool:
    if member is None:
        logger.info(
            "contacts: %s holds no contact role now but isn't in the server; "
            "nothing to kick",
            delegate.discord_user_id,
        )
        return False
    try:
        await member.kick(reason=reason)
    except discord.HTTPException:
        logger.exception(
            "contacts: couldn't kick %s after their last contact role went",
            delegate.discord_user_id,
        )
        return False
    await delegate_registry.mark_left(delegate.discord_user_id)
    logger.info(
        "contacts: kicked %s — no contact roles left after losing their slot",
        delegate.discord_user_id,
    )
    return True


def _member_for(
    delegate: Delegate, discord_guild: discord.Guild | None
) -> discord.Member | None:
    if discord_guild is None:
        return None
    return discord_guild.get_member(delegate.discord_user_id)


def _discord_role(guild: discord.Guild, role: str) -> discord.Role | None:
    role_id = contact_role_id(role)
    if not role_id:
        logger.warning("contacts: %s contact role ID is unset; Discord side skipped", role)
        return None
    discord_role = guild.get_role(role_id)
    if discord_role is None:
        logger.warning("contacts: %s contact role %s is not in the guild", role, role_id)
    return discord_role


async def _remove_role(member: discord.Member, role: str, *, reason: str) -> None:
    discord_role = _discord_role(member.guild, role)
    if discord_role is None:
        return
    try:
        await member.remove_roles(discord_role, reason=reason)
    except discord.HTTPException:
        logger.exception(
            "contacts: couldn't take the %s contact role off %s", role, member.id
        )


async def _grant_role(
    delegate: Delegate,
    role: str,
    *,
    discord_guild: discord.Guild | None,
    reason: str,
) -> None:
    member = _member_for(delegate, discord_guild)
    if member is None:
        logger.warning(
            "contacts: %s now holds the %s slot but isn't reachable in the "
            "server; Discord role not applied",
            delegate.discord_user_id,
            role,
        )
        return
    discord_role = _discord_role(member.guild, role)
    if discord_role is None:
        return
    if any(existing.id == discord_role.id for existing in member.roles):
        return
    await _add_role(member, discord_role, role=role, reason=reason)


async def _add_role(
    member: discord.Member, discord_role: discord.Role, *, role: str, reason: str
) -> None:
    try:
        await member.add_roles(discord_role, reason=reason)
    except discord.HTTPException:
        logger.exception(
            "contacts: couldn't give the %s contact role to %s", role, member.id
        )
