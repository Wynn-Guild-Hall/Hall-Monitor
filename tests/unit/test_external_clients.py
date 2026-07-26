"""Coverage for every third-party client Hall-Monitor talks to.

The shared :mod:`hall_monitor.external._client` wrapper is exercised
implicitly via each API module (retry, header propagation, 429 handling)
and directly for the priority-jump behaviour on :class:`_Bucket`.
"""

import asyncio

import httpx
import pytest

from hall_monitor.external import (
    mojang,
    playerdb,
    resolve_username_to_uuid,
    wynncraft,
    wynnpool,
)
from hall_monitor.external._client import _Bucket

MOJANG_URL = "https://api.mojang.com/users/profiles/minecraft/Notch"
PLAYERDB_URL = "https://playerdb.co/api/player/minecraft/Notch"


# --------------------------------------------------------------------------
# Mojang / PlayerDB / resolver
# --------------------------------------------------------------------------


async def test_mojang_happy_path(httpx_mock):
    httpx_mock.add_response(
        url=MOJANG_URL, json={"id": "069a79f444e94726a5befca90e38aaf5", "name": "Notch"}
    )
    assert await mojang.username_to_uuid("Notch") == "069a79f444e94726a5befca90e38aaf5"


async def test_mojang_404_returns_none(httpx_mock):
    httpx_mock.add_response(url=MOJANG_URL, status_code=404)
    assert await mojang.username_to_uuid("Notch") is None


async def test_playerdb_happy_path(httpx_mock):
    httpx_mock.add_response(
        url=PLAYERDB_URL,
        json={
            "success": True,
            "data": {
                "player": {
                    "raw_id": "069a79f444e94726a5befca90e38aaf5",
                    "username": "Notch",
                }
            },
        },
    )
    assert (
        await playerdb.username_to_uuid("Notch")
        == "069a79f444e94726a5befca90e38aaf5"
    )


async def test_playerdb_success_false_returns_none(httpx_mock):
    httpx_mock.add_response(url=PLAYERDB_URL, json={"success": False})
    assert await playerdb.username_to_uuid("Notch") is None


async def test_resolve_falls_back_on_mojang_429(httpx_mock):
    httpx_mock.add_response(url=MOJANG_URL, status_code=429, headers={"Retry-After": "0"})
    httpx_mock.add_response(
        url=PLAYERDB_URL,
        json={"success": True, "data": {"player": {"raw_id": "abc", "username": "Notch"}}},
    )
    assert await resolve_username_to_uuid("Notch") == "abc"


async def test_resolve_falls_back_on_connection_error(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=MOJANG_URL)
    httpx_mock.add_response(
        url=PLAYERDB_URL,
        json={"success": True, "data": {"player": {"raw_id": "abc", "username": "Notch"}}},
    )
    assert await resolve_username_to_uuid("Notch") == "abc"


async def test_resolve_does_not_fall_back_on_mojang_404(httpx_mock):
    """A 404 from Mojang is authoritative — the username doesn't exist.
    PlayerDB is not consulted, so no mock is registered for it."""
    httpx_mock.add_response(url=MOJANG_URL, status_code=404)
    assert await resolve_username_to_uuid("Notch") is None


# --------------------------------------------------------------------------
# Wynncraft
# --------------------------------------------------------------------------


async def test_wynncraft_sends_token_header_when_configured(httpx_mock, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", "secret-tok"
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/player/uuid",
        json={"guild": {"name": "Wynncraft Veterans", "prefix": "VETS", "rank": "CHIEF"}},
    )
    await wynncraft.get_player_guild("uuid")
    sent = httpx_mock.get_requests()[0]
    assert sent.headers.get("Authorization") == "Bearer secret-tok"


async def test_wynncraft_no_header_when_token_unset(httpx_mock, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/player/uuid",
        json={"guild": {"name": "n", "prefix": "N", "rank": "CHIEF"}},
    )
    await wynncraft.get_player_guild("uuid")
    sent = httpx_mock.get_requests()[0]
    assert "Authorization" not in sent.headers


async def test_wynncraft_get_player_guild_none_when_no_guild(httpx_mock):
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/player/uuid", json={"guild": None}
    )
    assert await wynncraft.get_player_guild("uuid") is None


async def test_wynncraft_get_guild_returns_typed_guild(httpx_mock):
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/guild/VETS",
        json={
            "uuid": "guild-uuid",
            "name": "Wynncraft Veterans",
            "prefix": "VETS",
            "level": 130,
            "territories": 50,
            "wars": 12345,
            "banner": {
                "base": "WHITE",
                "tier": 3,
                "layers": [{"colour": "RED", "pattern": "STRIPE_BOTTOM"}],
            },
            "members": {
                "total": 2,
                "owner": {"WenWeia": {"uuid": "owner-uuid"}},
                "chief": {"Alice": {"uuid": "alice-uuid"}},
                "strategist": {},
                "captain": {},
                "recruiter": {},
                "recruit": {},
            },
        },
    )
    guild = await wynncraft.get_guild("VETS")
    assert guild is not None
    assert guild.name == "Wynncraft Veterans"
    assert guild.prefix == "VETS"
    assert guild.level == 130
    assert guild.wars == 12345
    assert guild.banner is not None and guild.banner.base == "WHITE"
    assert guild.rank_of("owner-uuid") == "OWNER"
    assert guild.rank_of("alice-uuid") == "CHIEF"
    assert guild.rank_of("nobody") is None


async def test_wynncraft_get_guild_404_returns_none(httpx_mock):
    httpx_mock.add_response(url="https://api.wynncraft.com/v3/guild/NOPE", status_code=404)
    assert await wynncraft.get_guild("NOPE") is None


async def test_wynncraft_get_seasons(httpx_mock):
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/guild/seasons",
        json={
            "1": {"startDate": "2020-01-01T00:00:00Z", "endDate": "2020-02-01T00:00:00Z"},
            "2": {"startDate": "2020-02-01T00:00:00Z", "endDate": "2020-03-01T00:00:00Z"},
        },
    )
    seasons = await wynncraft.get_seasons()
    assert [s.number for s in seasons] == [1, 2]
    assert seasons[0].start.startswith("2020-01-01")


# --------------------------------------------------------------------------
# Wynnpool
# --------------------------------------------------------------------------


async def test_wynnpool_average_online_leaderboard(httpx_mock):
    httpx_mock.add_response(
        url="https://api.wynnpool.com/leaderboard/guild-average-online",
        json={
            "1": {"name": "Wynncraft Veterans", "prefix": "VETS", "averageOnline": 21.5},
            "2": {"name": "Other Guild", "prefix": "OTHR", "averageOnline": 18.2},
        },
    )
    result = await wynnpool.average_online_leaderboard()
    assert [(e.rank, e.tag, e.value) for e in result] == [
        (1, "VETS", 21.5),
        (2, "OTHR", 18.2),
    ]


async def test_wynnpool_guild_details_typed(httpx_mock):
    httpx_mock.add_response(
        url="https://api.wynnpool.com/guild/VETS",
        json={
            "name": "Wynncraft Veterans",
            "prefix": "VETS",
            "wars": 51234,
            "banner": {
                "base": "WHITE",
                "tier": 3,
                "layers": [{"colour": "RED", "pattern": "STRIPE_BOTTOM"}],
            },
        },
    )
    details = await wynnpool.guild_details("VETS")
    assert details is not None
    assert details.war_count == 51234
    assert details.banner is not None
    assert details.banner.layers[0].colour == "RED"


async def test_wynnpool_guild_details_404_returns_none(httpx_mock):
    httpx_mock.add_response(url="https://api.wynnpool.com/guild/NOPE", status_code=404)
    assert await wynnpool.guild_details("NOPE") is None


async def test_wynnpool_season_rating(httpx_mock):
    """Season leaderboards use a different shape: `ranking` array of
    `{rank, guild_uuid, guild_name, rating}` — no prefix/tag field."""
    httpx_mock.add_response(
        url="https://api.wynnpool.com/leaderboard/season-rating/7",
        json={
            "season": 7,
            "ranking": [
                {"rank": 1, "guild_uuid": "u1", "guild_name": "Sequoia", "rating": 15107093},
                {"rank": 2, "guild_uuid": "u2", "guild_name": "Aequitas", "rating": 14436545},
            ],
        },
    )
    result = await wynnpool.season_rating(7)
    assert [(e.rank, e.name, e.tag) for e in result] == [
        (1, "Sequoia", None),
        (2, "Aequitas", None),
    ]
    assert result[0].value == 15107093


# --------------------------------------------------------------------------
# Shared client — retry policy + priority jumping
# --------------------------------------------------------------------------


async def test_5xx_retried_once_then_succeeds(httpx_mock):
    httpx_mock.add_response(url=MOJANG_URL, status_code=500)
    httpx_mock.add_response(url=MOJANG_URL, json={"id": "abc", "name": "Notch"})
    assert await mojang.username_to_uuid("Notch") == "abc"


async def test_5xx_only_retried_once(httpx_mock):
    httpx_mock.add_response(url=MOJANG_URL, status_code=500)
    httpx_mock.add_response(url=MOJANG_URL, status_code=500)
    with pytest.raises(httpx.HTTPStatusError):
        await mojang.username_to_uuid("Notch")


async def test_bucket_priority_jumps_the_queue():
    """Urgent request enqueued after a non-urgent one runs first."""
    bucket = _Bucket()
    order: list[str] = []

    async def work(label: str, urgent: bool, hold: float = 0.0) -> None:
        release = await bucket.acquire(urgent)
        order.append(label)
        if hold:
            await asyncio.sleep(hold)
        release.release()

    # Occupy the bucket so subsequent acquires actually queue.
    first = asyncio.create_task(work("first", urgent=False, hold=0.02))
    await asyncio.sleep(0.001)  # let `first` acquire
    normal = asyncio.create_task(work("second-normal", urgent=False))
    urgent = asyncio.create_task(work("third-urgent", urgent=True))
    await asyncio.gather(first, normal, urgent)
    assert order == ["first", "third-urgent", "second-normal"]
