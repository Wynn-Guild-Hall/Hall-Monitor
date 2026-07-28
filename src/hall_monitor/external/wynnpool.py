"""Wynnpool API client — leaderboards + guild banner/warcount for notability."""

from dataclasses import dataclass

import httpx

from hall_monitor.config import settings

from ._client import Requester


_requester = Requester(base_url=settings.wynnpool_api_base)

# Banner pattern art is served from the website, not the API host, so it
# gets its own requester — and its own bucket, since a burst of pattern
# fetches shouldn't queue behind (or delay) a leaderboard sweep.
_web = Requester(base_url=settings.wynnpool_web_base)
_BUCKET_BANNERS = "wynnpool-banners"


@dataclass(frozen=True)
class LeaderboardEntry:
    """One row on a Wynnpool guild leaderboard.

    ``value`` carries the leaderboard-specific metric — e.g. the guild
    level for ``guildLevel``, the season rating for ``season-rating``, or
    the average online for ``guild-average-online``. ``None`` when the
    upstream payload doesn't expose it.

    ``tag`` (guild prefix) is present on the dict-keyed leaderboards but
    absent on ``season-rating`` (which only exposes ``guild_name`` /
    ``guild_uuid``), so it's optional. Notability's tag-based match will
    simply skip season entries whose tag is ``None`` — matching those by
    name would require a separate name→tag lookup we don't yet have.
    """

    rank: int
    name: str
    tag: str | None = None
    value: float | None = None


@dataclass(frozen=True)
class BannerLayer:
    colour: str
    pattern: str


@dataclass(frozen=True)
class Banner:
    base: str
    tier: int
    layers: tuple[BannerLayer, ...] = ()


@dataclass(frozen=True)
class GuildDetails:
    """The subset of Wynnpool's guild endpoint we consume.

    Wynnpool mirrors the same numbers Wynncraft publishes, on a far more
    forgiving rate limit, so this is the preferred source for the
    per-guild notability signals — see :func:`external.guild_stats`.
    Addressing is by guild *name*; there's no prefix endpoint.
    """

    name: str
    tag: str
    war_count: int
    banner: Banner | None = None
    territories: int = 0
    level: int | None = None


# Each board carries exactly one of these, so first-match is unambiguous.
_METRIC_FIELDS = ("level", "rating", "averageOnline", "wars", "territories", "value")


def _extract_value(row: dict) -> float | None:
    for key in _METRIC_FIELDS:
        if key in row and row[key] is not None:
            return float(row[key])
    return None


def _parse_leaderboard(payload) -> tuple[LeaderboardEntry, ...]:
    """Wynnpool ships two leaderboard shapes; this dispatches on them.

    1. **Dict-keyed by rank string**:
       ``{"1": {"uuid": ..., "name": ..., "prefix": "SEQ", "averageOnline": 16.29, ...}, ...}``
       Used by ``guild-average-online`` and ``guildLevel``.

    2. **Object with ranking array**:
       ``{"season": N, "ranking": [{"rank": 1, "guild_uuid": ..., "guild_name": "Sequoia", "rating": 15107093}, ...]}``
       Used by ``season-rating/{n}`` — note the ``guild_name`` / ``guild_uuid``
       field naming and the *absence* of any prefix/tag field.
    """
    entries: list[LeaderboardEntry] = []
    if isinstance(payload, dict) and "ranking" in payload:
        for row in payload.get("ranking", ()) or ():
            entries.append(
                LeaderboardEntry(
                    rank=int(row["rank"]),
                    name=row.get("guild_name") or row.get("name") or "",
                    tag=row.get("prefix"),  # season-rating omits this
                    value=_extract_value(row),
                )
            )
    elif isinstance(payload, dict):
        for rank_key, row in payload.items():
            try:
                rank = int(rank_key)
            except (TypeError, ValueError):
                continue
            entries.append(
                LeaderboardEntry(
                    rank=rank,
                    name=row.get("name", ""),
                    tag=row.get("prefix"),
                    value=_extract_value(row),
                )
            )
    return tuple(sorted(entries, key=lambda e: e.rank))


def _parse_banner(raw: dict | None) -> Banner | None:
    if not raw:
        return None
    layers = tuple(
        BannerLayer(colour=layer["colour"], pattern=layer["pattern"])
        for layer in raw.get("layers", [])
    )
    return Banner(base=raw["base"], tier=raw["tier"], layers=layers)


async def leaderboard(
    name: str, *, urgent: bool = False
) -> tuple[LeaderboardEntry, ...]:
    """Any Wynnpool guild leaderboard by its path segment.

    All of them are capped at 100 rows (50 for average-online), which is
    what makes them usable for threshold signals: if the board's floor
    sits below the threshold, absence from it proves the guild is under.
    """
    response = await _requester.get(f"/leaderboard/{name}", urgent=urgent)
    return _parse_leaderboard(response.json())


async def average_online_leaderboard(
    *, urgent: bool = False
) -> tuple[LeaderboardEntry, ...]:
    return await leaderboard("guild-average-online", urgent=urgent)


async def guild_level_leaderboard(
    *, urgent: bool = False
) -> tuple[LeaderboardEntry, ...]:
    return await leaderboard("guildLevel", urgent=urgent)


async def wars_leaderboard(*, urgent: bool = False) -> tuple[LeaderboardEntry, ...]:
    return await leaderboard("guildWars", urgent=urgent)


async def territories_leaderboard(
    *, urgent: bool = False
) -> tuple[LeaderboardEntry, ...]:
    return await leaderboard("guildTerritories", urgent=urgent)


async def season_rating(
    season: int, *, urgent: bool = False
) -> tuple[LeaderboardEntry, ...]:
    return await leaderboard(f"season-rating/{season}", urgent=urgent)


async def guild_details(
    guild_name: str, *, urgent: bool = False
) -> GuildDetails | None:
    """Returns the guild's banner and total war count. ``None`` on 404."""
    try:
        response = await _requester.get(f"/guild/{guild_name}", urgent=urgent)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    payload = response.json()
    return GuildDetails(
        name=payload["name"],
        tag=payload["prefix"],
        war_count=int(payload.get("wars") or 0),
        banner=_parse_banner(payload.get("banner")),
        territories=int(payload.get("territories") or 0),
        level=payload.get("level"),
    )


# Pattern art is static — a given pattern's SVG is the same forever — so
# it's cached for the life of the process. Thirty-four patterns at ~9 KB
# is nothing to hold, and re-fetching them per guild would turn one
# banner render into five requests.
_pattern_cache: dict[str, str | None] = {}


def reset_pattern_cache() -> None:
    """Drop the cached pattern art. For tests."""
    _pattern_cache.clear()


async def banner_pattern(pattern: str, *, urgent: bool = False) -> str | None:
    """The SVG source for one banner pattern, or ``None`` if there isn't one.

    ``None`` is a real answer, not an error: Wynnpool doesn't publish art
    for every pattern Minecraft has (``GLOBE`` and ``PIGLIN`` 404 today,
    as do ``DIAGONAL_UP_LEFT``/``RIGHT``), and a guild using one of those
    should still get a banner with its other layers rather than nothing.
    A miss is cached too, so a guild wearing a missing pattern doesn't
    cost a 404 on every render.
    """
    if pattern in _pattern_cache:
        return _pattern_cache[pattern]
    try:
        response = await _web.get(
            f"/banners/{pattern}.svg", bucket=_BUCKET_BANNERS, urgent=urgent
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        _pattern_cache[pattern] = None
        return None
    _pattern_cache[pattern] = response.text
    return response.text
