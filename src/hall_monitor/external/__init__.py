"""Third-party API clients + orchestrators that live above them."""

import logging
from dataclasses import dataclass

import httpx

from . import mojang, playerdb, wynncraft, wynnpool
from .mojang import Profile

logger = logging.getLogger(__name__)


async def resolve_profile(
    username: str, *, urgent: bool = False
) -> Profile | None:
    """Try Mojang first; fall back to PlayerDB on 429 or connection error.

    A 404 from Mojang is authoritative — the username genuinely doesn't
    exist, so we don't ask PlayerDB. Other HTTP errors (5xx, etc.) also
    fall through to PlayerDB so a Mojang outage doesn't block a join.
    """
    try:
        return await mojang.resolve_profile(username, urgent=urgent)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
    except httpx.RequestError:
        pass
    return await playerdb.resolve_profile(username, urgent=urgent)


async def resolve_username_to_uuid(
    username: str, *, urgent: bool = False
) -> str | None:
    """Convenience wrapper for callers that only need the UUID."""
    profile = await resolve_profile(username, urgent=urgent)
    return profile.uuid if profile else None


async def guild_name_for(tag: str, *, urgent: bool = False) -> str | None:
    """A guild's full name from its tag, or ``None`` if nothing knows it.

    **Wynncraft only**, unlike :func:`guild_stats`. Wynnpool addresses
    guilds by name and publishes no prefix route, so it can't answer the
    one question being asked here — the tag is all we have.

    Needed because the leaderboards are where names normally come from,
    and a guild can be notable without being on one of them: season
    boards carry no prefix, and a force override carries nothing at all.
    ``VETS`` is exactly that case, which is why the roster printed it as
    ``**VETS** (`VETS`)``.

    Errors are swallowed rather than raised. A name is decoration — the
    tag reads fine without it — and a 429 here must not cost a guild its
    notability evaluation.
    """
    try:
        guild = await wynncraft.get_guild_by_prefix(tag, urgent=urgent)
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        logger.debug("wynncraft prefix lookup failed for %s (%s)", tag, e)
        return None
    return guild.name if guild else None


@dataclass(frozen=True)
class GuildStats:
    """Per-guild numbers the notability signals need, plus who answered."""

    name: str
    tag: str
    territories: int
    wars: int | None
    source: str  # "wynnpool" or "wynncraft"


async def guild_stats(
    name: str | None, tag: str, *, urgent: bool = False
) -> GuildStats | None:
    """Territory count and war total, preferring Wynnpool.

    Wynnpool mirrors the same numbers on a much more forgiving rate limit,
    and the hourly notability sweep is the heaviest per-guild caller we
    have — pointing it at Wynncraft is what exhausts the shared anonymous
    limit. Wynncraft stays the authority, so anything Wynnpool can't
    answer falls through to it, **including a 404**: Wynnpool only knows
    guilds it has indexed, unlike Mojang, whose 404 we treat as final.

    Wynnpool addresses guilds by name only, so a tag with no known name
    goes straight to Wynncraft's prefix lookup.

    Wynnpool errors are swallowed (that's the fallback). Wynncraft errors
    propagate: a 429 there must not be recorded as "no territories, no
    wars", which would quietly read as a guild losing its notability.
    """
    if name:
        try:
            details = await wynnpool.guild_details(name, urgent=urgent)
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.debug("wynnpool guild lookup failed for %s (%s)", name, e)
        else:
            if details is not None:
                return GuildStats(
                    name=details.name,
                    tag=details.tag,
                    territories=details.territories,
                    wars=details.war_count,
                    source="wynnpool",
                )

    guild = await wynncraft.get_guild(name, urgent=urgent) if name else None
    if guild is None:
        guild = await wynncraft.get_guild_by_prefix(tag, urgent=urgent)
    if guild is None:
        return None
    return GuildStats(
        name=guild.name,
        tag=guild.prefix,
        territories=guild.territories,
        wars=guild.wars,
        source="wynncraft",
    )
