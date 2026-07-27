"""The per-guild aesthetic role: created on demand, coloured from Athena,
left alone when nothing changed, and recycled once it's holding nobody."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from hall_monitor.db.models import Delegate, GuildRole, PendingInvite
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
    can be searched by name or ID, and a ``create_role`` that appends."""

    def __init__(self, roles=()):
        self.roles = list(roles)
        self.create_role = AsyncMock(side_effect=self._create_role)

    async def _create_role(self, *, name, colour, mentionable, hoist, reason):
        role = _fake_role(len(self.roles) + 100, name, colour, mentionable=mentionable)
        self.roles.append(role)
        return role

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)


def _fake_role(role_id, name, colour="#000000", *, mentionable=True, members=()):
    role = MagicMock()
    role.id = role_id
    role.name = name
    role.colour = colour if isinstance(colour, discord.Colour) else _as_colour(colour)
    role.mentionable = mentionable
    role.members = list(members)
    role.edit = AsyncMock()
    role.delete = AsyncMock()
    return role


def _as_colour(hex_colour: str) -> discord.Colour:
    return discord.Colour(int(hex_colour.lstrip("#"), 16))


async def _owned(guild_tag: str, role) -> GuildRole:
    """The record `ensure_guild_role` writes when it creates a role."""
    return await GuildRole.create(guild_tag=guild_tag, discord_role_id=role.id)


# --------------------------------------------------------------------------
# ensure_guild_role
# --------------------------------------------------------------------------


async def test_creates_the_role_when_the_guild_has_none(db, athena):
    guild = FakeGuild()

    role = await guild_roles.ensure_guild_role(guild, "VETS")

    guild.create_role.assert_awaited_once()
    kwargs = guild.create_role.await_args.kwargs
    assert kwargs["name"] == "VETS"
    assert kwargs["colour"] == _as_colour(VISIBLE_RED)
    assert kwargs["mentionable"] is True  # `@VETS` has to work
    assert kwargs["hoist"] is False
    assert role.name == "VETS"


async def test_a_created_role_is_recorded_as_ours(db, athena):
    """Only roles we made may be recycled later, so making one says so."""
    guild = FakeGuild()

    role = await guild_roles.ensure_guild_role(guild, "VETS")

    assert (await GuildRole.get(guild_tag="VETS")).discord_role_id == role.id


async def test_an_adopted_role_is_not_recorded_as_ours(db, athena):
    """A role a human made stays theirs — recycling it would blank every
    past `@VETS` in the history to find out we were wrong."""
    guild = FakeGuild([_fake_role(1, "VETS", VISIBLE_RED)])

    await guild_roles.ensure_guild_role(guild, "VETS")

    assert await GuildRole.all().count() == 0


async def test_created_colour_is_the_discord_visible_variant(db, athena):
    """Not Athena's raw hue — that one is unreadable on the dark theme."""
    guild = FakeGuild()

    await guild_roles.ensure_guild_role(guild, "VETS")

    colour = guild.create_role.await_args.kwargs["colour"]
    assert colour == _as_colour(VISIBLE_RED)
    assert colour != _as_colour(ATHENA_RED)


async def test_adopts_an_existing_role_whatever_its_case(db, athena):
    """A role somebody made by hand as `Vets` is the VETS role."""
    existing = _fake_role(1, "Vets", VISIBLE_RED)
    guild = FakeGuild([existing])

    role = await guild_roles.ensure_guild_role(guild, "VETS")

    assert role is existing
    guild.create_role.assert_not_awaited()
    existing.edit.assert_not_awaited()  # colour already right


async def test_a_renamed_role_is_found_by_id_not_recreated(db, athena):
    """Decorating the role — `✦ VETS`, an emote prefix — must not spawn a
    second one beside it."""
    renamed = _fake_role(1, "✦ VETS", VISIBLE_RED)
    guild = FakeGuild([renamed])
    await _owned("VETS", renamed)

    role = await guild_roles.ensure_guild_role(guild, "VETS")

    assert role is renamed
    guild.create_role.assert_not_awaited()


async def test_the_name_is_never_written_back(db, athena):
    """The name belongs to whoever decorated it; we only own the colour."""
    renamed = _fake_role(1, "✦ VETS", "#123456")
    guild = FakeGuild([renamed])
    await _owned("VETS", renamed)

    await guild_roles.ensure_guild_role(guild, "VETS")

    assert "name" not in renamed.edit.await_args.kwargs


async def test_a_role_deleted_by_hand_is_forgotten_and_remade(db, athena):
    guild = FakeGuild()
    await GuildRole.create(guild_tag="VETS", discord_role_id=999)  # long gone

    role = await guild_roles.ensure_guild_role(guild, "VETS")

    guild.create_role.assert_awaited_once()
    assert (await GuildRole.get(guild_tag="VETS")).discord_role_id == role.id


async def test_updates_the_colour_when_athena_changes(db, athena):
    existing = _fake_role(1, "VETS", "#123456")
    guild = FakeGuild([existing])

    await guild_roles.ensure_guild_role(guild, "VETS")

    existing.edit.assert_awaited_once()
    assert existing.edit.await_args.kwargs["colour"] == _as_colour(VISIBLE_RED)
    guild.create_role.assert_not_awaited()


async def test_leaves_a_matching_role_completely_alone(db, athena):
    """No edit means no audit-log entry every time the sweep runs."""
    existing = _fake_role(1, "VETS", VISIBLE_RED)
    guild = FakeGuild([existing])

    await guild_roles.ensure_guild_role(guild, "VETS")

    existing.edit.assert_not_awaited()


async def test_makes_an_existing_role_mentionable(db, athena):
    existing = _fake_role(1, "VETS", VISIBLE_RED, mentionable=False)
    guild = FakeGuild([existing])

    await guild_roles.ensure_guild_role(guild, "VETS")

    assert existing.edit.await_args.kwargs["mentionable"] is True
    assert "colour" not in existing.edit.await_args.kwargs


async def test_unknown_guild_gets_the_default_colour(db, athena):
    """Athena doesn't index every guild; a delegate still gets a role."""
    guild = FakeGuild()

    await guild_roles.ensure_guild_role(guild, "NEWG")

    assert guild.create_role.await_args.kwargs["colour"] == _as_colour(
        athena_colour.DEFAULT_COLOUR
    )


async def test_explicit_colour_overrides_athena(db, athena):
    """Relegation clears a guild's colour without this module needing to
    know what notability is."""
    guild = FakeGuild()

    await guild_roles.ensure_guild_role(guild, "VETS", colour_hex="#000000")

    assert guild.create_role.await_args.kwargs["colour"] == discord.Colour(0)


async def test_create_failure_is_reported_not_raised(db, athena):
    guild = FakeGuild()
    guild.create_role = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "missing permissions")
    )

    assert await guild_roles.ensure_guild_role(guild, "VETS") is None
    assert await GuildRole.all().count() == 0, "nothing was created to own"


async def test_edit_failure_still_returns_the_role(db, athena):
    """The role exists and is usable; only its colour is stale."""
    existing = _fake_role(1, "VETS", "#123456")
    existing.edit = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "role too high")
    )
    guild = FakeGuild([existing])

    assert await guild_roles.ensure_guild_role(guild, "VETS") is existing


# --------------------------------------------------------------------------
# reconcile_role — colour, grey, recycle
# --------------------------------------------------------------------------


async def test_reconcile_colours_a_notable_guilds_role(db, athena):
    role = _fake_role(1, "VETS", "#000000", members=[MagicMock()])
    guild = FakeGuild([role])
    await _owned("VETS", role)

    assert (
        await guild_roles.reconcile_role(guild, "VETS", notable=True)
        == guild_roles.RECOLOURED
    )
    assert role.edit.await_args.kwargs["colour"] == _as_colour(VISIBLE_RED)


async def test_reconcile_greys_a_role_whose_guild_lost_notability(db, athena):
    """Members still wear it, so it isn't deleted — the colour going away
    says the same thing without breaking every past mention."""
    role = _fake_role(1, "VETS", VISIBLE_RED, members=[MagicMock()])
    guild = FakeGuild([role])
    await _owned("VETS", role)

    assert (
        await guild_roles.reconcile_role(guild, "VETS", notable=False)
        == guild_roles.GREYED
    )
    assert role.edit.await_args.kwargs["colour"] == discord.Colour.default()
    role.delete.assert_not_awaited()


async def test_reconcile_recycles_a_role_nobody_holds(db, athena):
    role = _fake_role(1, "VETS", VISIBLE_RED)
    guild = FakeGuild([role])
    await _owned("VETS", role)

    assert (
        await guild_roles.reconcile_role(guild, "VETS", notable=False)
        == guild_roles.DELETED
    )
    role.delete.assert_awaited_once()
    assert await GuildRole.all().count() == 0


async def test_reconcile_keeps_an_empty_role_while_a_delegate_remains(db, athena):
    """The member cache can lag a join; the delegate row can't."""
    role = _fake_role(1, "VETS", VISIBLE_RED)
    guild = FakeGuild([role])
    await _owned("VETS", role)
    await Delegate.create(mc_uuid="u", discord_user_id=5, guild_tag="VETS")

    await guild_roles.reconcile_role(guild, "VETS", notable=True)

    role.delete.assert_not_awaited()


async def test_reconcile_keeps_an_empty_role_while_a_verification_is_in_flight(
    db, athena
):
    """The join listener creates the role, then applies it. A sweep landing
    between the two must not delete it out from under `add_roles`."""
    role = _fake_role(1, "VETS", VISIBLE_RED)
    guild = FakeGuild([role])
    await _owned("VETS", role)
    await PendingInvite.create(
        mc_uuid="u", guild_tag="VETS", roles_bits=1, discord_invite_code="abc"
    )

    await guild_roles.reconcile_role(guild, "VETS", notable=True)

    role.delete.assert_not_awaited()


async def test_reconcile_never_recycles_a_role_we_didnt_create(db, athena):
    """Adopted by name, so it might be somebody's own."""
    role = _fake_role(1, "VETS", VISIBLE_RED)
    guild = FakeGuild([role])  # no GuildRole row

    assert (
        await guild_roles.reconcile_role(guild, "VETS", notable=False)
        == guild_roles.GREYED
    )
    role.delete.assert_not_awaited()


async def test_reconcile_reports_nothing_to_do(db, athena):
    role = _fake_role(1, "VETS", VISIBLE_RED, members=[MagicMock()])
    guild = FakeGuild([role])
    await _owned("VETS", role)

    assert (
        await guild_roles.reconcile_role(guild, "VETS", notable=True)
        == guild_roles.UNCHANGED
    )
    role.edit.assert_not_awaited()


async def test_reconcile_on_a_guild_with_no_role_does_nothing(db, athena):
    assert (
        await guild_roles.reconcile_role(FakeGuild(), "VETS", notable=True)
        == guild_roles.ABSENT
    )


async def test_reconcile_forgets_a_role_deleted_by_hand(db, athena):
    guild = FakeGuild()
    await GuildRole.create(guild_tag="VETS", discord_role_id=999)

    assert (
        await guild_roles.reconcile_role(guild, "VETS", notable=True)
        == guild_roles.ABSENT
    )
    assert await GuildRole.all().count() == 0


async def test_a_failed_delete_leaves_the_record_intact(db, athena):
    """Forgetting a role we couldn't delete would strand it forever."""
    role = _fake_role(1, "VETS", VISIBLE_RED)
    role.delete = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "role too high")
    )
    guild = FakeGuild([role])
    await _owned("VETS", role)

    await guild_roles.reconcile_role(guild, "VETS", notable=False)

    assert await GuildRole.all().count() == 1
