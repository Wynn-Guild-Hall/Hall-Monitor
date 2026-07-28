"""Coverage for every third-party client Hall-Monitor talks to.

The shared :mod:`hall_monitor.external._client` wrapper is exercised
implicitly via each API module (retry, header propagation, 429 handling)
and directly for the priority-jump behaviour on :class:`_Bucket`.
"""

import asyncio

import httpx
import pytest

from hall_monitor.external import (
    athena,
    guild_stats,
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
    assert await mojang.username_to_uuid("Notch") == "069a79f4-44e9-4726-a5be-fca90e38aaf5"


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
        == "069a79f4-44e9-4726-a5be-fca90e38aaf5"
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


# --------------------------------------------------------------------------
# guild_stats — Wynnpool preferred, Wynncraft authoritative
# --------------------------------------------------------------------------

WYNNPOOL_GUILD = "https://api.wynnpool.com/guild/Returners"
WYNNCRAFT_GUILD = "https://api.wynncraft.com/v3/guild/Returners"
WYNNCRAFT_PREFIX = "https://api.wynncraft.com/v3/guild/prefix/VETS"


def _wynncraft_payload(*, territories=5, wars=100):
    return {
        "uuid": "u",
        "name": "Returners",
        "prefix": "VETS",
        "level": 93,
        "territories": territories,
        "wars": wars,
        "members": {},
    }


async def test_guild_stats_prefers_wynnpool(httpx_mock):
    """No Wynncraft response is registered — if one were requested,
    pytest-httpx would fail the test, which is the assertion."""
    httpx_mock.add_response(
        url=WYNNPOOL_GUILD,
        json={"name": "Returners", "prefix": "VETS", "wars": 47, "territories": 3},
    )
    stats = await guild_stats("Returners", "VETS")
    assert stats is not None
    assert (stats.source, stats.wars, stats.territories) == ("wynnpool", 47, 3)


async def test_guild_stats_falls_back_when_wynnpool_404s(httpx_mock):
    """Unlike Mojang, a Wynnpool 404 isn't authoritative — it only means
    Wynnpool hasn't indexed the guild."""
    httpx_mock.add_response(url=WYNNPOOL_GUILD, status_code=404)
    httpx_mock.add_response(url=WYNNCRAFT_GUILD, json=_wynncraft_payload())
    stats = await guild_stats("Returners", "VETS")
    assert stats is not None
    assert (stats.source, stats.wars, stats.territories) == ("wynncraft", 100, 5)


async def test_guild_stats_falls_back_when_wynnpool_is_down(httpx_mock):
    httpx_mock.add_response(url=WYNNPOOL_GUILD, status_code=503)
    httpx_mock.add_response(url=WYNNPOOL_GUILD, status_code=503)  # one retry
    httpx_mock.add_response(url=WYNNCRAFT_GUILD, json=_wynncraft_payload())
    stats = await guild_stats("Returners", "VETS")
    assert stats is not None
    assert stats.source == "wynncraft"


async def test_guild_stats_skips_wynnpool_without_a_name(httpx_mock):
    """Wynnpool addresses guilds by name only, so a bare tag can't use it."""
    httpx_mock.add_response(url=WYNNCRAFT_PREFIX, json=_wynncraft_payload())
    stats = await guild_stats(None, "VETS")
    assert stats is not None
    assert stats.source == "wynncraft"


async def test_guild_stats_propagates_a_wynncraft_ratelimit(httpx_mock):
    """A 429 must not read as 'no territories, no wars' — that would quietly
    strip a guild of its major-guild status on the next refresh."""
    httpx_mock.add_response(url=WYNNPOOL_GUILD, status_code=404)
    httpx_mock.add_response(url=WYNNCRAFT_GUILD, status_code=429)
    with pytest.raises(httpx.HTTPStatusError):
        await guild_stats("Returners", "VETS")


async def test_guild_stats_none_when_neither_knows_the_guild(httpx_mock):
    httpx_mock.add_response(url=WYNNPOOL_GUILD, status_code=404)
    httpx_mock.add_response(url=WYNNCRAFT_GUILD, status_code=404)
    httpx_mock.add_response(url=WYNNCRAFT_PREFIX, status_code=404)
    assert await guild_stats("Returners", "VETS") is None


# --------------------------------------------------------------------------
# Outbound identification
# --------------------------------------------------------------------------


async def test_every_client_identifies_itself(httpx_mock):
    """Naming ourselves is the custom on the community APIs, and it's what
    lets their operators tell our traffic from an anonymous script's."""
    httpx_mock.add_response(url=MOJANG_URL, json={"id": "u", "name": "Notch"})
    await mojang.username_to_uuid("Notch")

    sent = httpx_mock.get_requests()[-1].headers["user-agent"]
    assert sent.startswith("hall-monitor/")
    assert "github.com/Wynn-Guild-Hall/Hall-Monitor" in sent
    assert "python-httpx" not in sent


async def test_wynnpool_sends_the_user_agent(httpx_mock):
    httpx_mock.add_response(
        url="https://api.wynnpool.com/leaderboard/guildLevel", json={}
    )
    await wynnpool.guild_level_leaderboard()
    assert httpx_mock.get_requests()[-1].headers["user-agent"].startswith("hall-monitor/")


async def test_a_caller_header_still_wins(httpx_mock, monkeypatch):
    """Wynncraft's bearer token is passed per request; the default headers
    must not get in the way of a caller setting its own."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", "tok"
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/guild/prefix/VETS", status_code=404
    )
    await wynncraft.get_guild_by_prefix("VETS")

    headers = httpx_mock.get_requests()[-1].headers
    assert headers["authorization"] == "Bearer tok"
    assert headers["user-agent"].startswith("hall-monitor/")


# --------------------------------------------------------------------------
# UUID canonicalisation
# --------------------------------------------------------------------------


def test_dashed_accepts_either_form():
    from hall_monitor.external._uuid import dashed

    bare = "085d0e5829d444aa837992a4568a59d6"
    canonical = "085d0e58-29d4-44aa-8379-92a4568a59d6"
    assert dashed(bare) == canonical
    assert dashed(canonical) == canonical
    assert dashed(canonical.upper()) == canonical


def test_dashed_passes_through_what_it_cannot_parse():
    """Fixtures use readable stand-ins; rejecting them isn't this
    function's job."""
    from hall_monitor.external._uuid import dashed

    assert dashed("uuid-chief") == "uuid-chief"


async def test_get_player_guild_dashes_the_uuid(httpx_mock):
    """Wynncraft's player route 404s on the bare form Mojang returns, and a
    404 here reads back as "no guild" — silently demoting a chief."""
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/player/085d0e58-29d4-44aa-8379-92a4568a59d6",
        json={"guild": {"name": "Returners", "prefix": "VETS", "rank": "CHIEF"}},
    )
    guild = await wynncraft.get_player_guild("085d0e5829d444aa837992a4568a59d6")
    assert guild is not None
    assert (guild.prefix, guild.rank) == ("VETS", "CHIEF")


async def test_get_player_guild_leaves_a_dashed_uuid_alone(httpx_mock):
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/player/085d0e58-29d4-44aa-8379-92a4568a59d6",
        json={"guild": None},
    )
    assert await wynncraft.get_player_guild("085d0e58-29d4-44aa-8379-92a4568a59d6") is None


async def test_resolver_output_is_ready_for_wynncraft(httpx_mock):
    """The end-to-end shape that broke /api/join/lookup: whatever the
    resolver returns has to be directly usable as a Wynncraft player key."""
    httpx_mock.add_response(
        url=MOJANG_URL, json={"id": "069a79f444e94726a5befca90e38aaf5", "name": "Notch"}
    )
    resolved = await resolve_username_to_uuid("Notch")
    httpx_mock.add_response(
        url=f"https://api.wynncraft.com/v3/player/{resolved}", json={"guild": None}
    )
    assert await wynncraft.get_player_guild(resolved) is None


# --------------------------------------------------------------------------
# Athena
# --------------------------------------------------------------------------


async def test_athena_guild_list_reads_the_real_row_shape(httpx_mock):
    """Athena keys its cache by name, so the guild name arrives as `_id` —
    indexing `name` raised KeyError against every real response."""
    httpx_mock.add_response(
        url="https://athena.wynntils.com/cache/get/guildList",
        json=[{"_id": "Returners", "prefix": "VETS", "color": "#e33232"}],
    )
    guilds = await athena.guild_list()
    assert guilds == (
        athena.AthenaGuild(name="Returners", prefix="VETS", colour="#e33232"),
    )


async def test_athena_blank_colour_reads_as_none(httpx_mock):
    """Roughly one guild in six has `color: ""`, not a missing key."""
    httpx_mock.add_response(
        url="https://athena.wynntils.com/cache/get/guildList",
        json=[{"_id": "A Wynnic Operation", "prefix": "WynO", "color": ""}],
    )
    assert (await athena.guild_list())[0].colour is None
