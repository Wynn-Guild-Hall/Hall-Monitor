"""Wynncraft v3 API client.

Reads ``WYNNCRAFT_API_TOKEN`` from Settings; when set, sends it as the auth
header. Works either way — the token just moves us off the shared anon
ratelimit onto a private application ratelimit.

Wynncraft splits its rate limit across multiple route families ("buckets");
we serialise ``/v3/player/*`` and ``/v3/guild/*`` separately so a bulk
guild sweep can't starve a player lookup.
"""

from dataclasses import dataclass, field

import httpx

from hall_monitor.config import settings

from ._client import Requester
from ._uuid import dashed

_BUCKET_PLAYER = "player"
_BUCKET_GUILD = "guild"


def _headers() -> dict[str, str]:
    if settings.wynncraft_api_token:
        return {"Authorization": f"Bearer {settings.wynncraft_api_token}"}
    return {}


_requester = Requester(base_url=settings.wynncraft_api_base)


@dataclass(frozen=True)
class PlayerGuild:
    """The ``.guild`` field on a Wynncraft player payload."""

    name: str
    prefix: str
    rank: str  # OWNER / CHIEF / STRATEGIST / CAPTAIN / RECRUITER / RECRUIT


@dataclass(frozen=True)
class Player:
    """A Wynncraft player — the canonical username plus their guild.

    The username comes free with the eligibility check we already make,
    and it's the only human-readable handle we have for a UUID. Storing
    it is what keeps a raw UUID off the `/join` page's conflict warning.
    """

    uuid: str
    username: str
    guild: "PlayerGuild | None"


@dataclass(frozen=True)
class GuildMember:
    """One row from the guild's members table."""

    username: str
    uuid: str
    rank: str


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
class Guild:
    """Trimmed Wynncraft guild payload — the fields Hall-Monitor uses.

    Notability reads ``level`` and ``territories``; join-eligibility walks
    ``members`` looking for the requester at OWNER/CHIEF rank; banner
    rendering (Stage 12) reads ``banner``.
    """

    uuid: str
    name: str
    prefix: str
    level: int
    territories: int
    members: tuple[GuildMember, ...]
    banner: Banner | None = None
    wars: int | None = None

    def rank_of(self, uuid: str) -> str | None:
        for m in self.members:
            if m.uuid == uuid:
                return m.rank
        return None


@dataclass(frozen=True)
class Season:
    number: int
    start: str  # ISO 8601 UTC (Wynncraft's `startDate`)
    end: str    # ISO 8601 UTC (Wynncraft's `endDate`)


def _parse_banner(raw: dict | None) -> Banner | None:
    if not raw:
        return None
    layers = tuple(
        BannerLayer(colour=layer["colour"], pattern=layer["pattern"])
        for layer in raw.get("layers", [])
    )
    return Banner(base=raw["base"], tier=raw["tier"], layers=layers)


_RANK_KEYS = ("owner", "chief", "strategist", "captain", "recruiter", "recruit")


def _parse_members(raw: dict) -> tuple[GuildMember, ...]:
    """The v3 guild endpoint returns members grouped by rank as a dict of
    ``{rank: {username: {uuid, ...}}}``. Flatten it into one tuple.
    """
    members: list[GuildMember] = []
    for rank in _RANK_KEYS:
        by_username = raw.get(rank) or {}
        for username, data in by_username.items():
            members.append(
                GuildMember(username=username, uuid=data["uuid"], rank=rank.upper())
            )
    return tuple(members)


async def get_player(uuid: str, *, urgent: bool = False) -> Player | None:
    """Username and current guild for a UUID, or ``None`` if unknown.

    The UUID is dashed on the way out: this route 404s on the bare
    32-character form, which reads back as "no guild" and quietly demotes
    a chief to nobody.
    """
    try:
        response = await _requester.get(
            f"/v3/player/{dashed(uuid)}",
            bucket=_BUCKET_PLAYER,
            urgent=urgent,
            headers=_headers(),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    payload = response.json()
    guild = payload.get("guild")
    return Player(
        uuid=dashed(uuid),
        username=payload.get("username") or "",
        guild=(
            PlayerGuild(name=guild["name"], prefix=guild["prefix"], rank=guild["rank"])
            if guild
            else None
        ),
    )


async def get_player_guild(uuid: str, *, urgent: bool = False) -> PlayerGuild | None:
    """The player's current guild, or ``None`` if they have none / the
    player is unknown."""
    player = await get_player(uuid, urgent=urgent)
    return player.guild if player else None


def _guild_from_payload(payload: dict) -> Guild:
    return Guild(
        uuid=payload["uuid"],
        name=payload["name"],
        prefix=payload["prefix"],
        level=payload["level"],
        territories=payload.get("territories", 0),
        members=_parse_members(payload.get("members") or {}),
        banner=_parse_banner(payload.get("banner")),
        wars=payload.get("wars"),
    )


async def get_guild(guild_name: str, *, urgent: bool = False) -> Guild | None:
    """Full guild by name (spaces allowed). Returns ``None`` on 404."""
    try:
        response = await _requester.get(
            f"/v3/guild/{guild_name}",
            bucket=_BUCKET_GUILD,
            urgent=urgent,
            headers=_headers(),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    return _guild_from_payload(response.json())


async def get_guild_by_prefix(tag: str, *, urgent: bool = False) -> Guild | None:
    """Full guild by prefix/tag. Returns ``None`` on 404."""
    try:
        response = await _requester.get(
            f"/v3/guild/prefix/{tag}",
            bucket=_BUCKET_GUILD,
            urgent=urgent,
            headers=_headers(),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    return _guild_from_payload(response.json())


async def get_seasons(*, urgent: bool = False) -> tuple[Season, ...]:
    """Every past + current season with its start/end timestamps."""
    response = await _requester.get(
        "/v3/guild/seasons",
        bucket=_BUCKET_GUILD,
        urgent=urgent,
        headers=_headers(),
    )
    payload = response.json() or {}
    seasons: list[Season] = []
    for key, data in payload.items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        seasons.append(
            Season(
                number=number,
                start=data["startDate"],
                end=data["endDate"],
            )
        )
    return tuple(sorted(seasons, key=lambda s: s.number))
