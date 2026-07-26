"""Athena guild-list lookup + Discord-visible colour normalisation."""

import pytest

from hall_monitor.services import athena_colour


async def test_lookup_matches_prefix_case_insensitively(httpx_mock):
    httpx_mock.add_response(
        url="https://athena.wynntils.com/cache/get/guildList",
        json=[
            {"name": "Wynncraft Veterans", "prefix": "VETS", "color": "#7f2727"},
            {"name": "Other", "prefix": "OTHR", "color": "#123456"},
        ],
    )
    assert await athena_colour.lookup("vets") == "#7f2727"


async def test_lookup_returns_none_when_unknown(httpx_mock):
    httpx_mock.add_response(
        url="https://athena.wynntils.com/cache/get/guildList",
        json=[{"name": "Other", "prefix": "OTHR", "color": "#123456"}],
    )
    assert await athena_colour.lookup("VETS") is None


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
