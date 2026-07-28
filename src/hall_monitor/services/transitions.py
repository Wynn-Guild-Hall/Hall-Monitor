"""Bring Discord in line with what the notability cache says.

This is a **reconcile**, not a set of edge-triggered transition handlers.
Nothing here diffs "was notable" against "is notable"; it reads the
current answer and makes Discord match. That distinction buys three
things: the pass is safe to run repeatedly, it heals a server that drifted
while the bot was down (or while a role edit 403'd), and it needs no
persisted "previous state" that could itself go stale.

What it settles per guild:

- **Contact roles.** The contact channels are for representatives of
  guilds currently in the Hall, so a guild that stops being notable has
  its contact roles withdrawn and gets them back — to the same people —
  when it returns. The ``GuildContact`` rows never move; see
  ``services/contacts.sync_contact_roles``.
- **The aesthetic role.** Coloured while notable, greyed while not, and
  deleted outright once it holds nobody and the guild has no delegates
  left. See ``services/guild_roles.reconcile_role``.
- **Each representative's standing.** Delegate while their guild is
  notable, Relegate while it isn't, External Relegate once they've moved
  to a different guild — the three are mutually exclusive, and an
  external rep also comes out of the guild's aesthetic role. Nobody is
  kicked and no row is deleted: guilds are expected to move in and out of
  the Hall, and the point of keeping the ``Delegate`` row is that coming
  back costs nothing.

Only guilds with a *presence* are considered — a role we made, a live
delegate, or a claimed contact slot. The notability cache knows a couple
of hundred guilds; all but a handful have nothing in this server to
reconcile, and creating roles for them is exactly what we don't want.

One guild's failure doesn't stop the pass: Discord errors are logged per
guild and the rest still get settled.
"""

import logging
from dataclasses import dataclass, field

import discord

from hall_monitor.db.models import Delegate, GuildContact, GuildRole
from hall_monitor.services import (
    contacts,
    delegate_registry,
    guild_roles,
    guild_tag as tags,
    notability,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settlement:
    """What settling one member left them with, and whether it took work."""

    standing: str
    guild_role: str | None
    changes: int

    def line(self) -> str:
        wearing = f"wearing `{self.guild_role}`" if self.guild_role else "no guild role"
        return f"standing `{self.standing}`, {wearing}"


@dataclass(frozen=True)
class ReconcileSummary:
    guilds: int = 0
    notable: int = 0
    contacts_changed: int = 0
    members_changed: int = 0
    roles: dict[str, int] = field(default_factory=dict)
    failed: int = 0

    def line(self) -> str:
        """One-line report for the scheduler log and `~script reconcile`."""
        roles = ", ".join(f"{count} {name}" for name, count in sorted(self.roles.items()))
        return (
            f"{self.guilds} guilds ({self.notable} notable), "
            f"{self.contacts_changed} contact role change(s), "
            f"{self.members_changed} standing change(s), "
            f"roles: {roles or 'nothing to do'}"
            + (f", {self.failed} failed" if self.failed else "")
        )


async def reconcile(discord_guild: discord.Guild) -> ReconcileSummary:
    """Settle every guild with a presence in the server against notability."""
    present = await guilds_present()
    notable_count = 0
    contacts_changed = 0
    outcomes: dict[str, int] = {}
    failed = 0

    members_changed = 0
    for tag in present:
        try:
            notable = await notability.is_notable(tag)
            contacts_changed += await contacts.sync_contact_roles(
                tag, discord_guild=discord_guild, granted=notable
            )
            outcome = await guild_roles.reconcile_role(
                discord_guild, tag, notable=notable
            )
            members_changed += await settle_members(
                discord_guild, tag, notable=notable
            )
        except Exception:  # noqa: BLE001 — one guild must not stop the sweep
            logger.exception("reconcile: %s failed; leaving it as it was", tag)
            failed += 1
            continue
        notable_count += notable
        if outcome != guild_roles.ABSENT:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

    return ReconcileSummary(
        guilds=len(present),
        notable=notable_count,
        contacts_changed=contacts_changed,
        members_changed=members_changed,
        roles=outcomes,
        failed=failed,
    )


async def settle_members(
    discord_guild: discord.Guild, guild_tag: str, *, notable: bool
) -> int:
    """Give a guild's representatives the standing their state implies.

    Membership is by who *represents* ``guild_tag`` (``~force guild`` can
    repoint that), not by which row they were written with — otherwise a
    repointed member would be settled by their old guild's pass, which is
    the one pass that shouldn't touch them.

    Three states, one role each: Delegate while the guild is notable,
    Relegate while it isn't, External Relegate for someone who has drifted
    to a guild they don't speak for. Plus the guild's aesthetic role,
    which the last of those loses.

    Nobody is kicked and no row is deleted. Losing notability is a state
    the Hall expects guilds to move in and out of, and the whole point of
    keeping the ``Delegate`` row is that coming back costs nothing.
    """
    mine = [
        delegate
        for delegate in await Delegate.filter(left_at=None)
        if tags.matches(
            await delegate_registry.represented_guild(delegate), guild_tag
        )
    ]
    if not mine:
        return 0

    role = await _guild_role(discord_guild, guild_tag, notable=notable)
    changed = 0
    for delegate in mine:
        member = discord_guild.get_member(delegate.discord_user_id)
        if member is None:
            continue  # left the server; the leave path owns that, not this
        changed += (await _settle(member, delegate, role, notable=notable)).changes
    return changed


async def settle_representative(
    discord_guild: discord.Guild, delegate: Delegate
) -> Settlement | None:
    """Settle one member against the guild they represent, and say what moved.

    The single-member counterpart to :func:`settle_members`, for commands
    that change one person's situation and have to report on it. A command
    whose effect only shows up at the next reconcile is indistinguishable
    from one that silently did nothing — which is how three bugs in a row
    got as far as production.
    """
    member = discord_guild.get_member(delegate.discord_user_id)
    if member is None:
        return None
    guild_tag = await delegate_registry.represented_guild(delegate)
    notable = await notability.is_notable(guild_tag)
    role = await _guild_role(discord_guild, guild_tag, notable=notable)
    return await _settle(member, delegate, role, notable=notable)


async def _guild_role(
    discord_guild: discord.Guild, guild_tag: str, *, notable: bool
) -> discord.Role | None:
    """The guild's aesthetic role, created if this is its first member.

    Minted here as well as at join, because a member repointed by
    ``~force guild`` needs their new guild's role to exist and nobody
    verified for it. Non-notable guilds get the flat colour rather than
    Athena's, so this can't undo the greying the reconcile just did.
    """
    return await guild_roles.ensure_guild_role(
        discord_guild, guild_tag, colour_hex=None if notable else "#000000"
    )


async def _settle(
    member: discord.Member,
    delegate: Delegate,
    role: discord.Role | None,
    *,
    notable: bool,
) -> Settlement:
    standing = await delegate_registry.standing(delegate, notable=notable)
    wears_it = standing != delegate_registry.EXTERNAL
    changes = 0
    changes += await guild_roles.sync_standing(member, standing)
    changes += await guild_roles.sync_guild_role_membership(
        member, role, wanted=wears_it
    )
    return Settlement(
        standing=standing,
        guild_role=role.name if role is not None and wears_it else None,
        changes=changes,
    )


async def guilds_present() -> list[str]:
    """Every guild tag with something in this server to reconcile.

    Deduplicated case-insensitively, keeping the first spelling seen —
    ``VETS`` and ``vets`` are one guild (``services/guild_tag.py``) and
    reconciling both would have the second undo the first.
    """
    live = await Delegate.filter(left_at=None)
    seen: dict[str, str] = {}
    for source in (
        await GuildRole.all().values_list("guild_tag", flat=True),
        [delegate.guild_tag for delegate in live],
        # The guild a `~force guild` points them at, which may have no
        # other presence here at all — and still needs its role minted and
        # its notability consulted.
        [await delegate_registry.represented_guild(delegate) for delegate in live],
        await GuildContact.all().values_list("guild_tag", flat=True),
    ):
        for tag in source:
            seen.setdefault(tags.normalise(tag), tag)
    return sorted(seen.values())
