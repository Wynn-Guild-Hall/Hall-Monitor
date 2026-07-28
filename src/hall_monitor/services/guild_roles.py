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

from hall_monitor.config import settings
from hall_monitor.db.models import Delegate, GuildRole, PendingInvite
from hall_monitor.services import (
    athena_colour,
    banner_render,
    delegate_registry,
    guild_tag as tags,
)

logger = logging.getLogger(__name__)

# Outcomes of a reconcile pass over one guild's role.
RECOLOURED = "recoloured"
GREYED = "greyed"
DELETED = "deleted"
UNCHANGED = "unchanged"
ABSENT = "absent"

# The three mutually-exclusive standing roles, and where their IDs live.
_STANDING_SETTINGS = {
    delegate_registry.DELEGATE: "delegate_role_id",
    delegate_registry.RELEGATE: "relegate_role_id",
    delegate_registry.EXTERNAL: "external_relegate_role_id",
}


def has_role_icons(discord_guild: discord.Guild) -> bool:
    """Whether this server is boosted enough to have role icons at all.

    Boost level 2 grants the `ROLE_ICONS` feature; below it every
    `display_icon` write is a 403. Reading the feature is how we tell
    "not allowed" from "failed", which are worth very different log
    lines when it happens sixty times an hour.
    """
    return "ROLE_ICONS" in getattr(discord_guild, "features", ())


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

    ``colour_hex`` overrides the Athena lookup, which is how relegation
    clears a guild's colour without teaching this module about major_guilds.

    Only the colour and the mention flag are ever written. The **name is
    left alone** once the role exists, so an operator is free to decorate
    it (`✦ VETS`, and one day a role icon) without this putting it back.
    """
    reason = reason or f"hall-monitor: {guild_tag} guild role"
    if colour_hex is None:
        colour_hex = await athena_colour.colour_for(guild_tag)
    colour = _colour(colour_hex)

    role = await resolve_role(discord_guild, guild_tag)
    if role is None:
        return await _create(discord_guild, guild_tag, colour, reason=reason)
    await _apply(role, colour, guild_tag=guild_tag, reason=reason)
    return role


async def resolve_role(
    discord_guild: discord.Guild, guild_tag: str
) -> discord.Role | None:
    """The guild's role: by recorded ID first, by name second.

    ID first because the name is the operator's to change. A role renamed
    to sit better in the list — decorated, prefixed, eventually carrying an
    emote — is still the same role, and matching only on name would quietly
    create a second one beside it.
    """
    owned = await GuildRole.filter(guild_tag__iexact=guild_tag).first()
    if owned is not None:
        role = discord_guild.get_role(owned.discord_role_id)
        if role is not None:
            return role
        await owned.delete()  # deleted by hand at some point; stop claiming it
    return find_guild_role(discord_guild, guild_tag)


async def reconcile_role(
    discord_guild: discord.Guild, guild_tag: str, *, major: bool
) -> str:
    """Bring one guild's role in line with the Hall, and recycle it if spent.

    Three outcomes worth naming:

    - **Deleted.** A role *we* created, holding nobody, for a guild with no
      live delegate and no verification in flight. It has no members to
      lose and no history worth keeping, and the next join recreates it —
      `ensure_guild_role` is create-on-demand — so leaving it would just
      spend one of Discord's 250 role slots on nothing.
    - **Greyed.** The guild isn't major but people still wear the role.
      Deleting it would blank every past `@TAG` in the channel history to
      `@deleted-role`; the colour going away says the same thing reversibly.
    - **Recoloured.** Major, so it carries the Athena hue.

    A role we didn't create is never deleted, only recoloured — a role
    adopted by name might be somebody's own, and by the time you find out
    the mentions are already broken.
    """
    role = await resolve_role(discord_guild, guild_tag)
    if role is None:
        return ABSENT

    owned = await GuildRole.filter(
        guild_tag__iexact=guild_tag, discord_role_id=role.id
    ).first()
    if owned is not None and not role.members and not await _in_use(guild_tag):
        return await _recycle(role, owned, guild_tag)

    colour = (
        _colour(await athena_colour.colour_for(guild_tag))
        if major
        else discord.Colour.default()
    )
    changed = await _apply(
        role,
        colour,
        guild_tag=guild_tag,
        reason=(
            f"hall-monitor: {guild_tag} is major"
            if major
            else f"hall-monitor: {guild_tag} is no longer major"
        ),
    )
    if not changed:
        return UNCHANGED
    return RECOLOURED if major else GREYED


async def forget_role_icons() -> int:
    """Drop every recorded icon hash. Returns how many were cleared.

    For a server that has fallen below boost level 2: Discord strips the
    icons from every role and says nothing, so the records have to catch
    up or they'd read as "already set" forever and the roles would stay
    bare through the next boost.

    A pure database operation — there is nothing to write to Discord
    below the threshold — which is what lets it run on a pass that
    fetches no banners at all.
    """
    stale = await GuildRole.filter(icon_hash__not_isnull=True).count()
    if stale:
        await GuildRole.filter(icon_hash__not_isnull=True).update(icon_hash=None)
        logger.info(
            "guild roles: forgot %d role icon(s) — the server is below the "
            "boost level that allows them",
            stale,
        )
    return stale


async def sync_role_icon(
    discord_guild: discord.Guild,
    role: discord.Role,
    guild_tag: str,
    *,
    icon: bytes | None,
) -> bool:
    """Put the guild's banner on its role as a display icon, or take it off.

    The icon shows immediately left of the name in the member list and in
    every ``@VETS`` mention, which is the only way a banner can sit beside
    a role name at all — a custom emote in a role *name* renders as literal
    text (§11).

    **Entirely optional.** Role icons need the server at boost level 2
    (`ROLE_ICONS`), and below that every write is a 403. So an unboosted
    server is detected and skipped rather than made to fail hourly, and a
    role without an icon works exactly as it did before.

    Written only when it differs, by hash: an unconditional edit per hour
    is an audit-log entry per hour. ``icon=None`` clears one we set, which
    is what an eviction from the emote budget should look like.

    Driven by ``services/emote_slots.py`` rather than by the role
    reconcile, because that module already holds the rendered bytes —
    rendering them a second time here would double the work for every
    guild in the budget, every hour.
    """
    owned = await GuildRole.filter(
        guild_tag__iexact=guild_tag, discord_role_id=role.id
    ).first()
    if owned is None:
        return False  # not ours to decorate

    if not has_role_icons(discord_guild):
        # Dropping below boost level 2 takes the icons off every role,
        # and nothing tells us — so a remembered hash would say "already
        # set" forever, and the roles would stay bare through the next
        # boost. Forgetting it is what makes regaining the level put them
        # back. Deliberately *not* an edit: there is nothing to write to.
        if owned.icon_hash is not None:
            owned.icon_hash = None
            await owned.save(update_fields=["icon_hash"])
            logger.info(
                "guild roles: %s lost its icon with the server's boost level",
                guild_tag,
            )
        return False

    digest = None if icon is None else banner_render.image_hash(icon)
    if owned.icon_hash == digest:
        return False

    try:
        await role.edit(
            display_icon=icon, reason=f"hall-monitor: {guild_tag} banner icon"
        )
    except discord.HTTPException:
        logger.exception("guild roles: couldn't set the %s role icon", guild_tag)
        return False
    owned.icon_hash = digest
    await owned.save(update_fields=["icon_hash"])
    logger.info(
        "guild roles: %s the %s role icon",
        "cleared" if icon is None else "updated",
        guild_tag,
    )
    return True


async def sync_standing(
    member: discord.Member, standing: str, *, reason: str | None = None
) -> bool:
    """Give ``member`` the one standing role they should have, and no other.

    Delegate, Relegate and External Relegate are mutually exclusive, so
    this both adds the wanted one and strips the other two — a member
    holding two of them reads as two different answers to the same
    question. Returns whether anything changed, so an hourly pass with
    nothing to do makes no requests.
    """
    wanted_id = getattr(settings, _STANDING_SETTINGS[standing])
    if not wanted_id:
        logger.warning(
            "guild roles: the %s role ID is unset; standing not applied", standing
        )
        return False
    wanted = member.guild.get_role(wanted_id)
    if wanted is None:
        logger.warning(
            "guild roles: %s role %s is not in the guild", standing, wanted_id
        )
        return False

    held = {role.id for role in member.roles}
    stale = [
        role
        for name, attribute in _STANDING_SETTINGS.items()
        if name != standing
        and (role_id := getattr(settings, attribute))
        and role_id in held
        and (role := member.guild.get_role(role_id)) is not None
    ]
    reason = reason or f"hall-monitor: standing is now {standing}"

    changed = False
    if wanted.id not in held:
        changed |= await _add(member, wanted, reason=reason)
    if stale:
        changed |= await _remove(member, stale, reason=reason)
    return changed


async def sync_guild_role_membership(
    member: discord.Member,
    role: discord.Role | None,
    *,
    wanted: bool,
    reason: str | None = None,
) -> bool:
    """Leave ``member`` wearing ``role`` and no other guild's role.

    Exactly one guild colour at a time, because the colour is a claim
    about who someone speaks for. Two of them is two claims; the wrong one
    is worse — a member repointed at ``ANO`` still showing ``VETS``
    misinforms every other guild in the room.

    ``wanted=False`` strips the lot: that's a representative who has
    drifted off to a guild they don't represent, and speaks for nobody
    until they come back.

    Only roles we created are stripped (``GuildRole``). A role adopted by
    name might be somebody's own, and taking it off a member is as
    unrecoverable for them as deleting it.
    """
    ours = {
        row.discord_role_id
        for row in await GuildRole.all()
        if role is None or row.discord_role_id != role.id
    }
    stale = [existing for existing in member.roles if existing.id in ours]
    holds = role is not None and any(
        existing.id == role.id for existing in member.roles
    )

    changed = False
    reason = reason or "hall-monitor: guild membership"
    if wanted and role is not None and not holds:
        changed |= await _add(member, role, reason=reason)
    if not wanted and holds:
        stale.append(role)
    if stale:
        changed |= await _remove(member, stale, reason=reason)
    return changed


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
    # Recorded *because* we made it: the reconcile pass deletes only roles
    # it can prove are ours.
    await GuildRole.update_or_create(
        guild_tag=guild_tag, defaults={"discord_role_id": role.id}
    )
    logger.info("guild roles: created the %s role in %s", guild_tag, colour)
    return role


async def _apply(
    role: discord.Role, colour: discord.Colour, *, guild_tag: str, reason: str
) -> bool:
    """Write only what differs. Returns whether anything was written."""
    changes: dict[str, object] = {}
    if role.colour != colour:
        changes["colour"] = colour
    if not role.mentionable:
        # `@VETS` is the point of the role; a role nobody may mention is
        # just a colour swatch.
        changes["mentionable"] = True
    if not changes:
        return False

    try:
        await role.edit(reason=reason, **changes)
    except discord.HTTPException:
        logger.exception(
            "guild roles: couldn't update the %s role (%s) — %s",
            guild_tag,
            role.id,
            ", ".join(sorted(changes)),
        )
        return False
    return True


async def _recycle(role: discord.Role, owned: GuildRole, guild_tag: str) -> str:
    try:
        await role.delete(reason=f"hall-monitor: {guild_tag} has nobody in the Hall")
    except discord.HTTPException:
        logger.exception("guild roles: couldn't delete the spent %s role", guild_tag)
        return UNCHANGED
    await owned.delete()
    logger.info(
        "guild roles: recycled the %s role — no members, no delegates", guild_tag
    )
    return DELETED


async def _add(member: discord.Member, role: discord.Role, *, reason: str) -> bool:
    try:
        await member.add_roles(role, reason=reason)
    except discord.HTTPException:
        logger.exception(
            "guild roles: couldn't give %s to %s", role.name, member.id
        )
        return False
    return True


async def _remove(
    member: discord.Member, roles: list[discord.Role], *, reason: str
) -> bool:
    try:
        await member.remove_roles(*roles, reason=reason)
    except discord.HTTPException:
        logger.exception(
            "guild roles: couldn't take %s off %s",
            ", ".join(role.name for role in roles),
            member.id,
        )
        return False
    return True


async def _in_use(guild_tag: str) -> bool:
    """Whether anyone is, or is about to be, a delegate for this guild.

    The pending-invite half closes a race with the join listener, which
    creates the role and then applies it: a sweep landing in between would
    delete the role out from under an `add_roles` that is already in
    flight, failing a verification. A `PendingInvite` exists for the whole
    of that window — it's only deleted once the `Delegate` row is written,
    and the row is what the first half of this check sees.
    """
    if await Delegate.filter(guild_tag__iexact=guild_tag, left_at=None).exists():
        return True
    return await PendingInvite.filter(guild_tag__iexact=guild_tag).exists()


def _colour(hex_colour: str) -> discord.Colour:
    return discord.Colour(int(hex_colour.strip().lstrip("#"), 16))
