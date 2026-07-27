"""Athena guild-list lookup, its cache, and Discord-visible normalisation."""

import httpx
import pytest

from hall_monitor.services import athena_colour

GUILD_LIST_URL = "https://athena.wynntils.com/cache/get/guildList"


@pytest.fixture(autouse=True)
def no_cache():
    """Module-level cache, so it has to be dropped between cases."""
    athena_colour.reset_cache()
    yield
    athena_colour.reset_cache()


async def test_lookup_matches_prefix_case_insensitively(httpx_mock):
    httpx_mock.add_response(
        url="https://athena.wynntils.com/cache/get/guildList",
        json=[
            {"_id": "Wynncraft Veterans", "prefix": "VETS", "color": "#7f2727"},
            {"_id": "Other", "prefix": "OTHR", "color": "#123456"},
        ],
    )
    assert await athena_colour.lookup("vets") == "#7f2727"


async def test_lookup_returns_none_when_unknown(httpx_mock):
    httpx_mock.add_response(
        url="https://athena.wynntils.com/cache/get/guildList",
        json=[{"_id": "Other", "prefix": "OTHR", "color": "#123456"}],
    )
    assert await athena_colour.lookup("VETS") is None


async def test_the_guild_list_is_fetched_once_per_ttl(httpx_mock):
    """Two lookups, one request — the list is every guild Athena knows,
    and the join path shouldn't pay for it twice."""
    httpx_mock.add_response(
        url=GUILD_LIST_URL,
        json=[{"_id": "Wynncraft Veterans", "prefix": "VETS", "color": "#7f2727"}],
    )

    assert await athena_colour.lookup("VETS") == "#7f2727"
    assert await athena_colour.lookup("VETS") == "#7f2727"
    assert len(httpx_mock.get_requests()) == 1


async def test_a_failed_refresh_serves_the_stale_list(httpx_mock, monkeypatch):
    """An hour-old colour beats every guild role going blurple because
    Athena had a bad minute."""
    httpx_mock.add_response(
        url=GUILD_LIST_URL,
        json=[{"_id": "Wynncraft Veterans", "prefix": "VETS", "color": "#7f2727"}],
    )
    assert await athena_colour.lookup("VETS") == "#7f2727"

    monkeypatch.setattr(athena_colour, "_LIST_TTL_S", 0)
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=GUILD_LIST_URL)

    assert await athena_colour.lookup("VETS") == "#7f2727"


async def test_colour_for_returns_the_visible_variant(httpx_mock):
    httpx_mock.add_response(
        url=GUILD_LIST_URL,
        json=[{"_id": "Wynncraft Veterans", "prefix": "VETS", "color": "#7f2727"}],
    )
    assert await athena_colour.colour_for("VETS") == "#9E2E2E"


async def test_colour_for_falls_back_when_athena_is_unreachable(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=GUILD_LIST_URL)
    assert await athena_colour.colour_for("VETS") == athena_colour.DEFAULT_COLOUR


async def test_colour_for_falls_back_on_an_unknown_guild(httpx_mock):
    httpx_mock.add_response(url=GUILD_LIST_URL, json=[])
    assert await athena_colour.colour_for("VETS") == athena_colour.DEFAULT_COLOUR


async def test_colour_for_falls_back_on_a_junk_colour(httpx_mock):
    """Athena's `color` is free text as far as we're concerned."""
    httpx_mock.add_response(
        url=GUILD_LIST_URL,
        json=[{"_id": "Wynncraft Veterans", "prefix": "VETS", "color": "red"}],
    )
    assert await athena_colour.colour_for("VETS") == athena_colour.DEFAULT_COLOUR


def test_the_default_colour_is_already_discord_visible():
    """Otherwise the fallback is a colour we'd have rejected from Athena."""
    assert (
        athena_colour.to_discord_visible(athena_colour.DEFAULT_COLOUR)
        == athena_colour.DEFAULT_COLOUR
    )


def test_to_discord_visible_lifts_near_black():
    """Very dark input gets its lightness pulled up to the min band."""
    result = athena_colour.to_discord_visible("#0A0A0A")
    r, g, b = int(result[1:3], 16), int(result[3:5], 16), int(result[5:7], 16)
    # ~40% grey should read on either theme.
    assert 90 <= r == g == b <= 115


def test_to_discord_visible_pulls_down_near_white():
    result = athena_colour.to_discord_visible("#FEFEFE")
    r, g, b = int(result[1:3], 16), int(result[3:5], 16), int(result[5:7], 16)
    assert 165 <= r == g == b <= 185  # ~70% grey band


def test_to_discord_visible_preserves_hue():
    """A dark red should come out as a lighter red, not a different colour."""
    result = athena_colour.to_discord_visible("#400000")
    r, g, b = int(result[1:3], 16), int(result[3:5], 16), int(result[5:7], 16)
    assert r > g and r > b
    assert g == b == 0 or g < 20


def test_to_discord_visible_accepts_hex_without_hash():
    assert athena_colour.to_discord_visible("400000") == athena_colour.to_discord_visible("#400000")


def test_to_discord_visible_rejects_bad_input():
    with pytest.raises(ValueError):
        athena_colour.to_discord_visible("nope")
