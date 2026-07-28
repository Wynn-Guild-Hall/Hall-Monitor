"""The join line in delegate general."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from hall_monitor.db.models import MajorGuildCache
from hall_monitor.services import announce

DELEGATE_CHANNEL_ID = 777


class FakeGuild:
    def __init__(self, channel=None):
        self.channel = channel
        self.id = 1

    def get_channel(self, channel_id):
        if self.channel is None or channel_id != DELEGATE_CHANNEL_ID:
            return None
        return self.channel


@pytest.fixture
def channel():
    sent = MagicMock()
    sent.send = AsyncMock()
    return sent


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.services.announce.settings.delegate_channel_id",
        DELEGATE_CHANNEL_ID,
    )


def _member(guild, user_id: int = 7):
    member = MagicMock()
    member.id = user_id
    member.mention = f"<@{user_id}>"
    member.guild = guild
    return member


def _said(channel) -> str:
    return channel.send.await_args.args[0]


# --------------------------------------------------------------------------
# Wording
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "roles,expected",
    [
        (set(), "joined as a representative of"),
        ({"events"}, "joined as the **events** representative of"),
        ({"events", "housing"}, "the **events** and **housing** representative"),
        (
            {"events", "housing", "warring"},
            "the **events**, **housing** and **warring** representative",
        ),
    ],
)
async def test_the_line_reads_as_a_sentence_for_any_number_of_roles(
    db, channel, roles, expected
):
    member = _member(FakeGuild(channel))

    await announce.joined(member, "Crrs", roles)

    assert expected in _said(channel)


async def test_it_names_the_guild_when_the_sweep_has_learned_it(db, channel):
    """The name comes from the cache the hourly sweep already fills, so
    this costs a query rather than a third-party request."""
    await MajorGuildCache.create(
        guild_tag="Crrs", is_major=True, signals_json="{}", guild_name="Corrosion"
    )
    member = _member(FakeGuild(channel))

    await announce.joined(member, "Crrs", {"events"})

    assert "**Corrosion** (`Crrs`)" in _said(channel)


async def test_an_unswept_guild_still_reads_correctly(db, channel):
    member = _member(FakeGuild(channel))

    await announce.joined(member, "Crrs", {"events"})

    assert "`Crrs`" in _said(channel)


async def test_an_observer_gets_a_different_sentence(db, channel):
    """A line that read like a representative's would have the room
    looking for a guild they don't have."""
    member = _member(FakeGuild(channel))

    await announce.joined_as_observer(member)

    body = _said(channel)
    assert "observer" in body
    assert "representative of" not in body


# --------------------------------------------------------------------------
# It never rings
# --------------------------------------------------------------------------


async def test_nobody_is_notified(db, channel):
    """This channel's one deliberate ping is the expel call. Pinging the
    new arrival would make their first experience of the Hall a
    notification about themselves."""
    member = _member(FakeGuild(channel))

    await announce.joined(member, "Crrs", {"events"})

    mentions = channel.send.await_args.kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False
    assert "<@7>" in _said(channel), "still renders as their name and links through"


# --------------------------------------------------------------------------
# When it can't
# --------------------------------------------------------------------------


async def test_an_unconfigured_channel_is_silent_not_an_error(db, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.services.announce.settings.delegate_channel_id", 0
    )
    member = _member(FakeGuild(None))

    assert await announce.joined(member, "Crrs", {"events"}) is False


async def test_a_missing_channel_warns_and_carries_on(db, caplog):
    member = _member(FakeGuild(None))

    assert await announce.joined(member, "Crrs", {"events"}) is False
    assert "not in the guild" in caplog.text


async def test_a_send_that_fails_doesnt_raise(db, channel):
    """A channel that can't be written to must not undo a verification
    that worked — the roles are already on."""
    channel.send.side_effect = discord.HTTPException(MagicMock(status=403), "no")
    member = _member(FakeGuild(channel))

    assert await announce.joined(member, "Crrs", {"events"}) is False
