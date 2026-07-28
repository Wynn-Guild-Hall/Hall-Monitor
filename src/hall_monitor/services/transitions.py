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
    """Give one guild's representatives the standing their state implies.

    Three states, one role each — Delegate while their guild is notable,
    Relegate while it isn't, External Relegate once they've moved to a
    different guild — plus membership of the guild's aesthetic role, which
    an external rep loses: wearing ``VETS`` while playing for someone else
    misinforms every other guild in the room.

    Nobody is kicked and no row is deleted. Losing notability is a state
    the Hall expects guilds to move in and out of, and the whole point of
    keeping the ``Delegate`` row is that coming back costs nothing.
    """
    role = await guild_roles.resolve_role(discord_guild, guild_tag)
    changed = 0
    for delegate in await Delegate.filter(guild_tag__iexact=guild_tag, left_at=None):
        member = discord_guild.get_member(delegate.discord_user_id)
        if member is None:
            continue  # left the server; the leave path owns that, not this
        standing = await delegate_registry.standing(delegate, notable=notable)
        changed += await guild_roles.sync_standing(member, standing)
        changed += await guild_roles.sync_guild_role_membership(
            member, role, wanted=standing != delegate_registry.EXTERNAL
        )
    return changed


async def guilds_present() -> list[str]:
    """Every guild tag with something in this server to reconcile.

    Deduplicated case-insensitively, keeping the first spelling seen —
    ``VETS`` and ``vets`` are one guild (``services/guild_tag.py``) and
    reconciling both would have the second undo the first.
    """
    seen: dict[str, str] = {}
    for source in (
        await GuildRole.all().values_list("guild_tag", flat=True),
        await Delegate.filter(left_at=None).values_list("guild_tag", flat=True),
        await GuildContact.all().values_list("guild_tag", flat=True),
    ):
        for tag in source:
            seen.setdefault(tags.normalise(tag), tag)
    return sorted(seen.values())
