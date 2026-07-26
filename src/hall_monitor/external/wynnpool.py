"""Wynnpool API client — leaderboards + guild banner/warcount for notability."""

from dataclasses import dataclass

import httpx

from hall_monitor.config import settings

from ._client import Requester


_requester = Requester(base_url=settings.wynnpool_api_base)


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
    """The subset of Wynnpool's guild endpoint we consume."""

    name: str
    tag: str
    war_count: int
    banner: Banner | None = None


_METRIC_FIELDS = ("level", "rating", "averageOnline", "value")


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


async def average_online_leaderboard(
    *, urgent: bool = False
) -> tuple[LeaderboardEntry, ...]:
    response = await _requester.get(
        "/leaderboard/guild-average-online", urgent=urgent
    )
    return _parse_leaderboard(response.json())


async def guild_level_leaderboard(
    *, urgent: bool = False
) -> tuple[LeaderboardEntry, ...]:
    response = await _requester.get("/leaderboard/guildLevel", urgent=urgent)
    return _parse_leaderboard(response.json())


async def season_rating(
    season: int, *, urgent: bool = False
) -> tuple[LeaderboardEntry, ...]:
    response = await _requester.get(
        f"/leaderboard/season-rating/{season}", urgent=urgent
    )
    return _parse_leaderboard(response.json())


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
    )
