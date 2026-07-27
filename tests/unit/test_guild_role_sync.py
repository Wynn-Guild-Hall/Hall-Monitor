"""The per-guild aesthetic role: created on demand, coloured from Athena,
left alone when nothing changed."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from hall_monitor.services import athena_colour, guild_roles

# Athena's own VETS red, and what `to_discord_visible` makes of it: too
# dark to read on Discord's dark theme, so lightness comes up to the 0.40
# floor and saturation to 0.55. Pinned as a literal — a transform nobody
# can predict the output of is one nobody can review.
ATHENA_RED = "#7f2727"
VISIBLE_RED = "#9E2E2E"


@pytest.fixture(autouse=True)
def no_athena_cache():
    athena_colour.reset_cache()
    yield
    athena_colour.reset_cache()


@pytest.fixture
def athena(monkeypatch):
    """Athena answers with one colour per tag; unknown tags return ``None``."""
    colours = {"VETS": ATHENA_RED}

    async def fake_lookup(guild_tag, *, urgent=False):
        return colours.get(guild_tag.upper())

    monkeypatch.setattr(athena_colour, "lookup", fake_lookup)
    return colours


class FakeGuild:
    """Just enough ``discord.Guild`` for the role service: a role list it
    can be searched by name, and a ``create_role`` that appends to it."""

    def __init__(self, roles=()):
        self.roles = list(roles)
        self.create_role = AsyncMock(side_effect=self._create_role)

    async def _create_role(self, *, name, colour, mentionable, hoist, reason):
        role = _fake_role(len(self.roles) + 100, name, colour, mentionable=mentionable)
        self.roles.append(role)
        return role


def _fake_role(role_id, name, colour="#000000", *, mentionable=True):
    role = MagicMock()
    role.id = role_id
    role.name = name
    role.colour = (
        colour if isinstance(colour, discord.Colour) else _as_colour(colour)
    )
    role.mentionable = mentionable
    role.edit = AsyncMock()
    return role


def _as_colour(hex_colour: str) -> discord.Colour:
    return discord.Colour(int(hex_colour.lstrip("#"), 16))


# --------------------------------------------------------------------------
# ensure_guild_role
# --------------------------------------------------------------------------


async def test_creates_the_role_when_the_guild_has_none(athena):
    guild = FakeGuild()

    role = await guild_roles.ensure_guild_role(guild, "VETS")

    guild.create_role.assert_awaited_once()
    kwargs = guild.create_role.await_args.kwargs
    assert kwargs["name"] == "VETS"
    assert kwargs["colour"] == _as_colour(VISIBLE_RED)
    assert kwargs["mentionable"] is True  # `@VETS` has to work
    assert kwargs["hoist"] is False
    assert role.name == "VETS"


async def test_created_colour_is_the_discord_visible_variant(athena):
    """Not Athena's raw hue — that one is unreadable on the dark theme."""
    guild = FakeGuild()

    await guild_roles.ensure_guild_role(guild, "VETS")

    colour = guild.create_role.await_args.kwargs["colour"]
    assert colour == _as_colour(VISIBLE_RED)
    assert colour != _as_colour(ATHENA_RED)


async def test_adopts_an_existing_role_whatever_its_case(athena):
    """A role somebody made by hand as `Vets` is the VETS role."""
    existing = _fake_role(1, "Vets", VISIBLE_RED)
    guild = FakeGuild([existing])

    role = await guild_roles.ensure_guild_role(guild, "VETS")

    assert role is existing
    guild.create_role.assert_not_awaited()
    existing.edit.assert_not_awaited()  # colour already right


async def test_updates_the_colour_when_athena_changes(athena):
    existing = _fake_role(1, "VETS", "#123456")
    guild = FakeGuild([existing])

    await guild_roles.ensure_guild_role(guild, "VETS")

    existing.edit.assert_awaited_once()
    assert existing.edit.await_args.kwargs["colour"] == _as_colour(VISIBLE_RED)
    guild.create_role.assert_not_awaited()


async def test_leaves_a_matching_role_completely_alone(athena):
    """No edit means no audit-log entry every time the sweep runs."""
    existing = _fake_role(1, "VETS", VISIBLE_RED)
    guild = FakeGuild([existing])

    await guild_roles.ensure_guild_role(guild, "VETS")

    existing.edit.assert_not_awaited()


async def test_makes_an_existing_role_mentionable(athena):
    existing = _fake_role(1, "VETS", VISIBLE_RED, mentionable=False)
    guild = FakeGuild([existing])

    await guild_roles.ensure_guild_role(guild, "VETS")

    assert existing.edit.await_args.kwargs["mentionable"] is True
    assert "colour" not in existing.edit.await_args.kwargs


async def test_unknown_guild_gets_the_default_colour(athena):
    """Athena doesn't index every guild; a delegate still gets a role."""
    guild = FakeGuild()

    await guild_roles.ensure_guild_role(guild, "NEWG")

    assert guild.create_role.await_args.kwargs["colour"] == _as_colour(
        athena_colour.DEFAULT_COLOUR
    )


async def test_explicit_colour_overrides_athena(athena):
    """Stage 9 clears a relegated guild's colour without this module
    needing to know what notability is."""
    guild = FakeGuild()

    await guild_roles.ensure_guild_role(guild, "VETS", colour_hex="#000000")

    assert guild.create_role.await_args.kwargs["colour"] == discord.Colour(0)


async def test_create_failure_is_reported_not_raised(athena):
    guild = FakeGuild()
    guild.create_role = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "missing permissions")
    )

    assert await guild_roles.ensure_guild_role(guild, "VETS") is None


async def test_edit_failure_still_returns_the_role(athena):
    """The role exists and is usable; only its colour is stale."""
    existing = _fake_role(1, "VETS", "#123456")
    existing.edit = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "role too high")
    )
    guild = FakeGuild([existing])

    assert await guild_roles.ensure_guild_role(guild, "VETS") is existing
