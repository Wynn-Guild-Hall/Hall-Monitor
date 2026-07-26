"""Wynnpool API client — leaderboards + guild banner/warcount for notability."""

from dataclasses import dataclass

import httpx

from hall_monitor.config import settings

from ._client import Requester


_requester = Requester(base_url=settings.wynnpool_api_base)


@dataclass(frozen=True)
class LeaderboardEntry:
    """One row on a Wynnpool guild leaderboard."""

    rank: int
    name: str
    tag: str


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


def _parse_leaderboard(payload) -> tuple[LeaderboardEntry, ...]:
    """Wynnpool leaderboards may come as a list or a dict keyed by rank."""
    rows = payload.values() if isinstance(payload, dict) else payload or ()
    entries: list[LeaderboardEntry] = []
    for row in rows:
        entries.append(
            LeaderboardEntry(
                rank=int(row["rank"]),
                name=row["name"],
                tag=row["tag"],
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
        tag=payload["tag"],
        war_count=int(payload.get("warCount", 0)),
        banner=_parse_banner(payload.get("banner")),
    )
