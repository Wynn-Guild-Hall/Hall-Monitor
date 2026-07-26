"""Per-role contact assignment with uniqueness enforcement.

One contact per role per guild; assigning a new one displaces the old one,
and members with no remaining contact roles are kicked from the server.
Displacement / kick logic lands in Stage 7 — this file currently ships the
read side plus the contact-role-name → Discord-role-ID mapping the join
listener applies.
"""

import discord

from hall_monitor.config import settings
from hall_monitor.db.models import Delegate, GuildContact
from hall_monitor.services import delegate_registry

CONTACT_ROLES = ("events", "housing", "warring", "ownership")

_CONTACT_ROLE_SETTINGS = {
    "events": "events_contact_role_id",
    "housing": "housing_contact_role_id",
    "warring": "warring_contact_role_id",
    "ownership": "ownership_contact_role_id",
}


class UnknownContactRole(KeyError):
    """Raised for a role name outside :data:`CONTACT_ROLES`."""


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
    rows = await GuildContact.filter(guild_tag=guild_tag).prefetch_related("delegate")
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


async def assign_contact(guild_tag: str, role: str, delegate) -> None:
    raise NotImplementedError  # Stage 7


async def current_holder(guild_tag: str, role: str):
    raise NotImplementedError  # Stage 7


async def resolve_conflicts_and_kick_if_empty(
    guild_tag: str, roles: set[str], new_holder
) -> None:
    raise NotImplementedError  # Stage 7
