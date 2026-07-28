"""Persistent MC UUID ↔ Discord user bindings for guild representatives.

``left_at`` is the DB-side "member still in the server?" tri-state:

- ``NULL``  — believed to still be a member.
- non-NULL — the ``on_member_remove`` listener saw them leave (Stage 11+).

``is_current_member`` cross-checks against Discord when a bot guild is
available, so the DB not being caught up yet doesn't produce a false
positive between the leave event and the listener firing.

Two guild tags, deliberately: ``guild_tag`` is the guild they verified as
a representative of and never changes, while ``current_guild_tag`` is
where Wynncraft last saw them and is refreshed by
:func:`refresh_current_guilds`. A representative who moves guilds stops
representing the old one — see :data:`EXTERNAL` — but they don't start
representing the new one, which would need them to be a chief of it and
to verify again.
"""

import logging
from datetime import datetime, timezone

import discord
import httpx

from hall_monitor.db.models import Delegate
from hall_monitor.external import wynncraft
from hall_monitor.services import guild_tag as tags

logger = logging.getLogger(__name__)

# What a member's standing role should be. Three states, one role each.
DELEGATE = "delegate"
RELEGATE = "relegate"
EXTERNAL = "external"


async def get_by_mc_uuid(mc_uuid: str) -> Delegate | None:
    return await Delegate.get_or_none(mc_uuid=mc_uuid)


async def get_by_discord_user_id(discord_user_id: int) -> Delegate | None:
    return await Delegate.get_or_none(discord_user_id=discord_user_id)


async def register(
    mc_uuid: str,
    discord_user_id: int,
    guild_tag: str,
    mc_username: str | None = None,
) -> Delegate:
    """Create or reactivate a Delegate row for this MC UUID.

    Reactivation clears ``left_at`` so a returning delegate isn't confused
    with a stale one. A ``None`` username leaves any stored one alone —
    forgetting a name we already had would be a downgrade.

    ``current_guild_tag`` is seeded from the same value: verification
    proves they're a chief of that guild right now, so starting them as
    "unknown" until the next hourly poll would only invite a wrong answer
    in between.
    """
    defaults = {
        "discord_user_id": discord_user_id,
        "guild_tag": guild_tag,
        "current_guild_tag": guild_tag,
        "left_at": None,
    }
    if mc_username:
        defaults["mc_username"] = mc_username
    delegate, _ = await Delegate.update_or_create(mc_uuid=mc_uuid, defaults=defaults)
    return delegate


async def display_name(delegate: Delegate) -> str:
    """A human-readable handle for a delegate, resolved once and kept.

    Rows written before the username was stored resolve on first use and
    save the answer, so the cost is one lookup per delegate ever rather
    than one per page view. A UUID is the last resort, not the default.
    """
    if delegate.mc_username:
        return delegate.mc_username
    player = await wynncraft.get_player(delegate.mc_uuid, urgent=True)
    if player is None or not player.username:
        return delegate.mc_uuid
    delegate.mc_username = player.username
    await delegate.save(update_fields=["mc_username"])
    return player.username


def is_external(delegate: Delegate) -> bool:
    """Whether this representative has moved to a different guild.

    Being *guildless* doesn't count, per the design brief: someone between
    guilds is still the person their guild sent, and the alternative is
    relegating anyone who leaves for an afternoon. Only actively
    representing one guild while sitting in another does.
    """
    if not delegate.current_guild_tag:
        return False
    return not tags.matches(delegate.current_guild_tag, delegate.guild_tag)


def standing(delegate: Delegate, *, notable: bool) -> str:
    """Which of the three standing roles this delegate should be wearing.

    Moving guilds outranks the guild's own notability: a rep who's left
    can't be promoted back by their old guild having a good month.
    """
    if is_external(delegate):
        return EXTERNAL
    return DELEGATE if notable else RELEGATE


async def refresh_current_guilds() -> tuple[int, int]:
    """Ask Wynncraft where every live delegate currently is.

    Wynncraft pushes nothing, so this is a poll: one request per delegate,
    hourly, serialised on the player bucket. Returns
    ``(checked, external)`` for the log.

    A lookup that fails leaves the stored value alone rather than
    resetting it — a 429 recorded as "no guild" would read back as the
    delegate having *rejoined* their guild, quietly promoting someone the
    last sweep had relegated.
    """
    checked = external = 0
    for delegate in await Delegate.filter(left_at=None):
        try:
            guild = await wynncraft.get_player_guild(delegate.mc_uuid)
        except (httpx.HTTPError, KeyError, ValueError):
            logger.warning(
                "guild watch: couldn't look up %s; keeping %r",
                delegate.mc_uuid,
                delegate.current_guild_tag,
                exc_info=True,
            )
            continue
        checked += 1
        tag = guild.prefix if guild else None
        if delegate.current_guild_tag != tag:
            delegate.current_guild_tag = tag
            await delegate.save(update_fields=["current_guild_tag"])
            logger.info(
                "guild watch: %s is now in %s (represents %s)",
                delegate.mc_uuid,
                tag or "no guild",
                delegate.guild_tag,
            )
        external += is_external(delegate)
    return checked, external


async def mark_left(discord_user_id: int) -> None:
    await Delegate.filter(discord_user_id=discord_user_id).update(
        left_at=datetime.now(timezone.utc)
    )


async def is_current_member(
    mc_uuid: str, discord_guild: discord.Guild | None = None
) -> bool:
    """True iff a Delegate row exists for this UUID and the user is still
    in ``discord_guild``. Falls back to the DB ``left_at`` field when no
    guild handle is available (tests, or the bot not being connected yet)."""
    delegate = await get_by_mc_uuid(mc_uuid)
    if delegate is None or delegate.left_at is not None:
        return False
    if discord_guild is None:
        return True
    return discord_guild.get_member(delegate.discord_user_id) is not None
