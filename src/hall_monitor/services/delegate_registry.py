"""Persistent MC UUID ↔ Discord user bindings for guild representatives.

``left_at`` is the DB-side "member still in the server?" tri-state:

- ``NULL``  — believed to still be a member.
- non-NULL — the ``on_member_remove`` listener saw them leave (Stage 11+).

``is_current_member`` cross-checks against Discord when a bot guild is
available, so the DB not being caught up yet doesn't produce a false
positive between the leave event and the listener firing.

Two guild tags on the row, plus an override, and they answer different
questions:

- ``guild_tag`` — the guild they verified as a chief of. Never changes.
- ``current_guild_tag`` — where Wynncraft last saw them, refreshed hourly
  by :func:`refresh_current_guilds`.
- ``ForceOverride(kind="guild")`` — a janitor asserting who they
  represent, which outranks both.

:func:`represented_guild` folds those into the one answer everything
downstream keys on. A representative who drifts to a different guild on
their own stops representing the old one (:data:`EXTERNAL`) and doesn't
start representing the new one — that would need them to be a chief of it
and to verify again, or a janitor to say so with ``~force guild``.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import discord
import httpx

from hall_monitor.db.models import Delegate, ForceOverride
from hall_monitor.external import wynncraft
from hall_monitor.services import guild_tag as tags

logger = logging.getLogger(__name__)

# What a member's standing role should be. Three states, one role each.
DELEGATE = "delegate"
RELEGATE = "relegate"
EXTERNAL = "external"

# `ForceOverride.kind` for `~force guild`.
FORCE_GUILD = "guild"

# The in-game ranks that may speak for a guild. One copy: the join
# lookup, the MC-time verify route and `~force rep` all ask the same
# question, and three spellings of it is three things to forget to
# change — the same reasoning as `time_parse.gating_rejection`.
ELIGIBLE_RANKS = frozenset({"OWNER", "CHIEF"})


async def eligible_guild(mc_uuid: str, *, urgent: bool = True):
    """The guild this account may represent, or ``None``.

    ``None`` covers every way of not being eligible — guildless, an
    ordinary member, or a lookup that failed — because none of them
    entitles anyone to anything and the caller's answer is the same.
    Callers that need to *explain* the refusal should ask
    :func:`hall_monitor.external.wynncraft.get_player_guild` themselves.

    ``urgent`` by default: every caller is a person waiting, in
    Minecraft or in a browser or on a command.
    """
    guild = await wynncraft.get_player_guild(mc_uuid, urgent=urgent)
    if guild is None or guild.rank not in ELIGIBLE_RANKS:
        return None
    return guild


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


async def forced_guild(discord_user_id: int) -> str | None:
    """A live ``~force guild`` override for this member, if there is one.

    Expired rows read as absent; they're left in place rather than swept,
    so a janitor can see what was forced and when it ran out.
    """
    override = await ForceOverride.filter(
        kind=FORCE_GUILD, subject=str(discord_user_id)
    ).first()
    if override is None:
        return None
    if override.expires_at is not None and override.expires_at <= datetime.now(
        timezone.utc
    ):
        return None
    return json.loads(override.payload_json or "{}").get("guild_tag") or None


async def set_forced_guild(
    discord_user_id: int, guild_tag: str, expires_at: datetime | None
) -> None:
    """Record where a member is to be treated as playing, until ``expires_at``.

    One row per member — re-forcing replaces, because two rows would mean
    the second `~force guild` silently did nothing.
    """
    payload = json.dumps({"guild_tag": guild_tag})
    await ForceOverride.update_or_create(
        kind=FORCE_GUILD,
        subject=str(discord_user_id),
        defaults={"payload_json": payload, "expires_at": expires_at},
    )


async def clear_forced_guild(discord_user_id: int) -> int:
    """Drop the override; returns how many rows went."""
    return await ForceOverride.filter(
        kind=FORCE_GUILD, subject=str(discord_user_id)
    ).delete()


async def represented_guild(delegate: Delegate) -> str:
    """Which guild this member speaks for, right now.

    Normally the one they verified as a chief of. A live ``~force guild``
    replaces it outright — the janitor is asserting who they represent,
    and everything downstream follows from that single answer: the tag on
    their nickname, the colour they wear, which guild's contact slots they
    may hold, and whose notability decides their standing.

    The override can't live in ``current_guild_tag``: the watch rewrites
    that column every hour from Wynncraft, so a forced value stored there
    would survive exactly until the next sweep.
    """
    return await forced_guild(delegate.discord_user_id) or delegate.guild_tag


async def is_external(delegate: Delegate) -> bool:
    """Whether this representative has drifted away from the guild they speak for.

    Being *guildless* doesn't count, per the design brief: someone between
    guilds is still the person their guild sent, and the alternative is
    relegating anyone who leaves for an afternoon. Only actively
    representing one guild while sitting in another does.

    **A forced representation is never external.** The watch reporting a
    different guild is exactly the situation the janitor overrode; letting
    it relegate them anyway would make the override useless in the case it
    exists for.
    """
    if await forced_guild(delegate.discord_user_id):
        return False
    if not delegate.current_guild_tag:
        return False
    return not tags.matches(delegate.current_guild_tag, delegate.guild_tag)


async def standing(delegate: Delegate, *, notable: bool) -> str:
    """Which of the three standing roles this delegate should be wearing.

    Moving guilds outranks the guild's own notability: a rep who's left
    can't be promoted back by their old guild having a good month.
    """
    if await is_external(delegate):
        return EXTERNAL
    return DELEGATE if notable else RELEGATE


# How long a delegate may go unchecked before it's worth telling someone.
# The design brief's guarantee is that a guild change is noticed within
# 48h; the poll is hourly, so anything near this bound means the poll
# itself has been failing rather than that it hasn't come round yet.
STALE_AFTER = timedelta(hours=48)


async def refresh_current_guilds() -> tuple[int, int]:
    """Ask Wynncraft where every live delegate currently is.

    Wynncraft pushes nothing, so this is a poll: one request per delegate,
    hourly, serialised on the player bucket. Returns
    ``(checked, external)`` for the log.

    A lookup that fails leaves the stored value alone rather than
    resetting it — a 429 recorded as "no guild" would read back as the
    delegate having *rejoined* their guild, quietly promoting someone the
    last sweep had relegated.

    It does **not** leave ``current_guild_checked_at`` alone, and that's
    the whole point of the column: keeping the last known guild is right
    per-call but unbounded across calls, so a persistent failure would
    otherwise freeze every delegate's guild with nothing to see it by.
    The timestamp only moves on an answer.
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
        changed = delegate.current_guild_tag != tag
        delegate.current_guild_tag = tag
        delegate.current_guild_checked_at = datetime.now(timezone.utc)
        await delegate.save(
            update_fields=["current_guild_tag", "current_guild_checked_at"]
        )
        if changed:
            logger.info(
                "guild watch: %s is now in %s (represents %s)",
                delegate.mc_uuid,
                tag or "no guild",
                delegate.guild_tag,
            )
        external += await is_external(delegate)

    stale = await stale_delegates()
    if stale:
        logger.warning(
            "guild watch: %d delegate(s) haven't been checked in %dh — "
            "`~script delegate_status` for which",
            len(stale),
            STALE_AFTER.total_seconds() // 3600,
        )
    return checked, external


async def stale_delegates(within: timedelta | None = None) -> list[Delegate]:
    """Live delegates the watch hasn't successfully checked inside ``within``.

    A delegate that has *never* been checked counts as stale once they're
    older than the window — a row written moments ago simply hasn't had
    its first sweep yet, and reporting it would make a fresh verification
    look like a fault.
    """
    window = within or STALE_AFTER
    cutoff = datetime.now(timezone.utc) - window
    return [
        delegate
        for delegate in await Delegate.filter(left_at=None)
        if (
            delegate.current_guild_checked_at is None
            and delegate.joined_at < cutoff
        )
        or (
            delegate.current_guild_checked_at is not None
            and delegate.current_guild_checked_at < cutoff
        )
    ]


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
