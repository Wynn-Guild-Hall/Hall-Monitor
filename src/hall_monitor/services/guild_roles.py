"""The per-guild aesthetic role: one Discord role named for the guild tag.

Every delegate wears their guild's tag as a coloured role, so a glance at
the member list says which guilds are in the room and `@VETS` reaches
their representatives. The colour comes from Athena — the same hue
Wynntils renders the guild in — pushed through
``athena_colour.to_discord_visible`` so it reads on both Discord themes.

The role is created on demand and matched by name, case-insensitively
(``services/guild_tag.py``), so a role somebody made by hand as ``Vets``
is adopted rather than duplicated. New roles land at the bottom of the
hierarchy, which is where ``create_role`` puts them and where an
aesthetic role belongs: it grants nothing, and a role above the bot's own
would be one it couldn't manage afterwards.

Discord-side failures are logged rather than raised, matching
``services/contacts.py``. A delegate missing their guild colour is
cosmetic; a verification that unwound because a role edit 403'd is not.
"""

import logging

import discord

from hall_monitor.services import athena_colour, guild_tag as tags

logger = logging.getLogger(__name__)


def find_guild_role(
    discord_guild: discord.Guild, guild_tag: str
) -> discord.Role | None:
    """The existing role for ``guild_tag``, matched by name. ``None`` if absent."""
    for role in discord_guild.roles:
        if tags.matches(role.name, guild_tag):
            return role
    return None


async def ensure_guild_role(
    discord_guild: discord.Guild,
    guild_tag: str,
    *,
    colour_hex: str | None = None,
    reason: str | None = None,
) -> discord.Role | None:
    """Create or update ``guild_tag``'s aesthetic role; return it.

    Returns ``None`` only when the role doesn't exist and couldn't be
    created. An existing role whose colour still matches Athena's is left
    strictly alone — no edit, no request, no audit-log entry every hour.

    ``colour_hex`` overrides the Athena lookup, which is how Stage 9's
    relegation clears a guild's colour without teaching this module about
    notability.
    """
    reason = reason or f"hall-monitor: {guild_tag} guild role"
    if colour_hex is None:
        colour_hex = await athena_colour.colour_for(guild_tag)
    colour = _colour(colour_hex)

    role = find_guild_role(discord_guild, guild_tag)
    if role is None:
        return await _create(discord_guild, guild_tag, colour, reason=reason)

    changes: dict[str, object] = {}
    if role.colour != colour:
        changes["colour"] = colour
    if not role.mentionable:
        # `@VETS` is the point of the role; a role nobody may mention is
        # just a colour swatch.
        changes["mentionable"] = True
    if not changes:
        return role

    try:
        await role.edit(reason=reason, **changes)
    except discord.HTTPException:
        logger.exception(
            "guild roles: couldn't update the %s role (%s) — %s",
            guild_tag,
            role.id,
            ", ".join(sorted(changes)),
        )
    return role


async def set_delegate(member) -> None:
    raise NotImplementedError


async def set_relegate(member) -> None:
    raise NotImplementedError


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


async def _create(
    discord_guild: discord.Guild,
    guild_tag: str,
    colour: discord.Colour,
    *,
    reason: str,
) -> discord.Role | None:
    try:
        role = await discord_guild.create_role(
            name=guild_tag,
            colour=colour,
            mentionable=True,
            hoist=False,
            reason=reason,
        )
    except discord.HTTPException:
        logger.exception("guild roles: couldn't create the %s role", guild_tag)
        return None
    logger.info("guild roles: created the %s role in %s", guild_tag, colour)
    return role


def _colour(hex_colour: str) -> discord.Colour:
    return discord.Colour(int(hex_colour.strip().lstrip("#"), 16))
