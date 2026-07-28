"""Resolving the member somebody meant.

The case that prompted this: `~force assign Struppi0508 warring` came
back as a bare usage line. `Struppi0508` is a *Minecraft* name, and
discord.py matches Discord names exactly — so it lost twice over, once
to the ` [Volc]` suffix this bot writes onto every nickname and once to
Discord's lowercase handles.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ext import commands

from hall_monitor.db.models import Delegate, Observer
from hall_monitor.discord_bot.converters import HallMember


def _member(user_id, *, name, nick=None, global_name=None):
    member = MagicMock()
    member.id = user_id
    member.name = name
    member.nick = nick
    member.global_name = global_name
    member.__str__ = lambda self: nick or name
    return member


class FakeGuild:
    def __init__(self, *members):
        self.members = list(members)
        # The gateway search discord.py falls back to, plus the cache
        # lookup it tries alongside. Both answering "nobody" is what
        # makes the fallback a genuine dead end in these cases.
        self.query_members = AsyncMock(return_value=[])
        self.get_member_named = MagicMock(return_value=None)

    def get_member(self, user_id):
        return next((m for m in self.members if m.id == user_id), None)


def _ctx(guild):
    ctx = MagicMock()
    ctx.guild = guild
    ctx.bot = MagicMock()
    ctx.message.mentions = []
    return ctx


async def _convert(guild, argument):
    return await HallMember().convert(_ctx(guild), argument)


@pytest.fixture
def struppi():
    """As they appear in the server: our nickname, Discord's lowercase
    handle, and a Minecraft name matching neither exactly."""
    return _member(
        7, name="struppi_0508", nick="Struppi0508 [Volc]", global_name="Struppi"
    )


# --------------------------------------------------------------------------
# The name that started it
# --------------------------------------------------------------------------


async def test_a_minecraft_name_resolves(db, struppi):
    """The name the room uses, and the one in the nickname prefix."""
    await Delegate.create(
        mc_uuid="u1", mc_username="Struppi0508", discord_user_id=7, guild_tag="Volc"
    )

    assert await _convert(FakeGuild(struppi), "Struppi0508") is struppi


async def test_a_minecraft_name_folds_case(db, struppi):
    await Delegate.create(
        mc_uuid="u1", mc_username="Struppi0508", discord_user_id=7, guild_tag="Volc"
    )

    assert await _convert(FakeGuild(struppi), "struppi0508") is struppi


async def test_an_observers_minecraft_name_resolves_too(db):
    """They have an account on file for exactly this sort of question."""
    watcher = _member(9, name="someone", nick="Wisteria")
    await Observer.create(
        mc_uuid="u2", mc_username="wisteriablossoms", discord_user_id=9
    )

    assert await _convert(FakeGuild(watcher), "WisteriaBlossoms") is watcher


# --------------------------------------------------------------------------
# The two things that defeated discord.py
# --------------------------------------------------------------------------


async def test_the_nickname_matches_without_our_tag_suffix(db, struppi):
    """The suffix is ours (§13), and it's what stood between a typed name
    and a match."""
    assert await _convert(FakeGuild(struppi), "Struppi0508") is struppi


async def test_the_whole_display_name_pasted_in_still_matches(db, struppi):
    """Copying it out of the member list gives you the tag as well."""
    assert await _convert(FakeGuild(struppi), "Struppi0508 [Volc]") is struppi


async def test_a_username_matches_case_insensitively(db, struppi):
    """Discord's newer handles are lowercase, so an operator typing what
    they see would otherwise miss."""
    assert await _convert(FakeGuild(struppi), "Struppi_0508") is struppi


async def test_a_global_name_matches(db, struppi):
    assert await _convert(FakeGuild(struppi), "struppi") is struppi


# --------------------------------------------------------------------------
# Mentions and IDs
# --------------------------------------------------------------------------


async def test_a_mention_resolves_without_touching_the_gateway(db, struppi):
    guild = FakeGuild(struppi)

    assert await _convert(guild, "<@7>") is struppi
    guild.query_members.assert_not_awaited()


async def test_a_nickname_mention_resolves(db, struppi):
    assert await _convert(FakeGuild(struppi), "<@!7>") is struppi


async def test_a_raw_snowflake_resolves(db):
    them = _member(123456789012345678, name="x")

    assert await _convert(FakeGuild(them), "123456789012345678") is them


async def test_a_mention_for_somebody_absent_says_so_rather_than_searching(db):
    """An explicit mention names one person and no other. Falling through
    to a name search would go looking for somebody else entirely."""
    guild = FakeGuild()

    with pytest.raises(commands.BadArgument, match="isn't a member"):
        await _convert(guild, "<@404>")
    guild.query_members.assert_not_awaited()


async def test_a_short_number_is_treated_as_a_name(db):
    """A member could be called `12`, so a bare number only reads as an
    ID when it looks like a snowflake."""
    them = _member(5, name="12")

    assert await _convert(FakeGuild(them), "12") is them


# --------------------------------------------------------------------------
# When it can't tell
# --------------------------------------------------------------------------


async def test_two_matches_are_refused_and_named(db):
    """Assigning a contact slot to the wrong person is quiet and annoying
    to undo, and the operator is right there to disambiguate."""
    one = _member(1, name="alex")
    two = _member(2, name="other", nick="Alex [VETS]")

    with pytest.raises(commands.BadArgument, match="more than one member"):
        await _convert(FakeGuild(one, two), "alex")


async def test_an_unknown_name_says_what_to_try(db):
    guild = FakeGuild(_member(1, name="somebody"))

    with pytest.raises(commands.BadArgument, match="Minecraft name"):
        await _convert(guild, "nobodyatall")


async def test_the_gateway_is_the_last_resort_not_the_first(db, struppi):
    """Its name search is a round-trip, and in this server it's both
    slower than the lookups above and less likely to succeed."""
    await Delegate.create(
        mc_uuid="u1", mc_username="Struppi0508", discord_user_id=7, guild_tag="Volc"
    )
    guild = FakeGuild(struppi)

    await _convert(guild, "Struppi0508")

    guild.query_members.assert_not_awaited()


async def test_a_dm_is_refused(db):
    ctx = _ctx(None)

    with pytest.raises(commands.BadArgument, match="inside the server"):
        await HallMember().convert(ctx, "anyone")
