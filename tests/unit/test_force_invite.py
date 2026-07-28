"""`~force invite` — a representative invite minted on a monitor's word.

The command exists to skip the one check `~force rep` refuses to skip,
so most of these are about what it *doesn't* stop doing: the guild tag
is still verified, a banned guild is still refused, and the reply still
says out loud what wasn't checked.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from hall_monitor.db.models import Delegate, ExpelBan, Observer, PendingInvite
from hall_monitor.discord_bot.cogs.force import invite as force_invite
from hall_monitor.services import discord_invites, role_bits


class FakeGuild:
    def __init__(self):
        self.id = 1
        self.members = {}
        self.channel = MagicMock()
        minted = MagicMock()
        minted.code = "inv123"
        self.channel.create_invite = AsyncMock(return_value=minted)

    def get_channel(self, _id):
        return self.channel

    def get_member(self, user_id):
        return self.members.get(user_id)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.force.invite.settings.welcome_channel_id", 2
    )
    discord_invites.reset_snapshot()
    yield
    discord_invites.reset_snapshot()


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def mojang(monkeypatch):
    known = {}

    async def fake(username, *, urgent=False):
        return known.get(username.lower())

    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.force.invite.resolve_profile", fake
    )
    return known


@pytest.fixture
def wynncraft_knows(monkeypatch):
    """Which guild tags resolve to a name."""
    names = {"crrs": "Corrosion"}

    async def fake(tag, *, urgent=False):
        return names.get(tag.strip().casefold())

    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.force.invite.external.guild_name_for", fake
    )
    return names


def _profile(username, uuid):
    profile = MagicMock()
    profile.username = username
    profile.uuid = uuid
    return profile


def _ctx(guild):
    ctx = MagicMock()
    ctx.guild = guild
    ctx.author = MagicMock()
    ctx.author.id = 999
    ctx.bot = MagicMock()
    ctx.bot.http.delete_invite = AsyncMock()
    ctx.reply = AsyncMock()
    return ctx


@pytest.fixture
def wisteria(mojang):
    mojang["wisteriablossoms"] = _profile("wisteriablossoms", "uuid-wist")
    return "wisteriablossoms"


# --------------------------------------------------------------------------
# Minting
# --------------------------------------------------------------------------


async def test_it_mints_a_week_long_invite_with_the_asked_for_roles(
    db, guild, wisteria, wynncraft_knows
):
    reply = await force_invite.hand_out(_ctx(guild), wisteria, "Crrs", ("events",))

    row = await PendingInvite.get(mc_uuid="uuid-wist")
    assert row.guild_tag == "Crrs"
    assert role_bits.decode(row.roles_bits) == {"events"}
    assert row.expires_at is not None
    assert guild.channel.create_invite.await_args.kwargs["max_age"] == (
        discord_invites.HANDED_INVITE_MAX_AGE_SECONDS
    )
    assert "inv123" in reply


async def test_it_accepts_several_roles(db, guild, wisteria, wynncraft_knows):
    await force_invite.hand_out(_ctx(guild), wisteria, "Crrs", ("events", "housing"))

    row = await PendingInvite.get(mc_uuid="uuid-wist")
    assert role_bits.decode(row.roles_bits) == {"events", "housing"}


async def test_no_roles_means_the_delegate_role_alone(
    db, guild, wisteria, wynncraft_knows
):
    """The same thing `HALL00` means from Minecraft — rare, but a real
    answer for somebody who belongs in the room without a slot."""
    reply = await force_invite.hand_out(_ctx(guild), wisteria, "Crrs", ())

    assert (await PendingInvite.get(mc_uuid="uuid-wist")).roles_bits == 0
    assert "delegate role alone" in reply


async def test_the_reply_says_what_it_didnt_check(
    db, guild, wisteria, wynncraft_knows
):
    """The whole point of the command is skipping the chief check, so the
    reply has to be honest that it did — and about what happens if they
    aren't in that guild at all."""
    reply = await force_invite.hand_out(_ctx(guild), wisteria, "Crrs", ("events",))

    assert "did **not** check whether they're a chief" in reply
    assert "External" in reply
    assert "Corrosion" in reply, "the resolved name, so a typo'd tag is obvious"


# --------------------------------------------------------------------------
# What it still refuses
# --------------------------------------------------------------------------


async def test_an_unknown_guild_tag_is_refused(db, guild, wisteria, wynncraft_knows):
    """The one thing it *can* verify. A typo would mint a real invite for
    a guild that doesn't exist, and the first sign of it would be a role
    and a roster entry nobody ordered."""
    reply = await force_invite.hand_out(_ctx(guild), wisteria, "Crss", ("events",))

    assert "doesn't know a guild with the tag" in reply
    assert not await PendingInvite.exists()


async def test_a_banned_guild_is_refused(db, guild, wisteria, wynncraft_knows):
    await ExpelBan.create(guild_tag="Crrs", reason="voted out")

    reply = await force_invite.hand_out(_ctx(guild), wisteria, "Crrs", ("events",))

    assert "barred from the Hall" in reply
    assert not await PendingInvite.exists()


async def test_an_unknown_role_is_refused_with_the_valid_set(
    db, guild, wisteria, wynncraft_knows
):
    reply = await force_invite.hand_out(_ctx(guild), wisteria, "Crrs", ("catering",))

    assert "isn't a contact role" in reply
    assert "`events`" in reply
    assert not await PendingInvite.exists()


async def test_an_unknown_username_is_refused(db, guild, mojang, wynncraft_knows):
    reply = await force_invite.hand_out(_ctx(guild), "nobody", "Crrs", ("events",))

    assert "isn't a Minecraft account" in reply
    assert not await PendingInvite.exists()


async def test_an_existing_representative_is_pointed_at_the_right_commands(
    db, guild, wisteria, wynncraft_knows
):
    await Delegate.create(mc_uuid="uuid-wist", discord_user_id=5, guild_tag="VETS")
    guild.members[5] = MagicMock()

    reply = await force_invite.hand_out(_ctx(guild), wisteria, "Crrs", ("events",))

    assert "already a representative" in reply
    assert "~force rep" in reply and "~force assign" in reply


async def test_an_observer_is_told_the_invite_would_do_nothing(
    db, guild, wisteria, wynncraft_knows
):
    """They're already in the server, so clicking it fires no join event —
    the same dead end `~force rep` exists to unblock."""
    await Observer.create(
        mc_uuid="uuid-wist", mc_username="wisteriablossoms", discord_user_id=7
    )
    guild.members[7] = MagicMock()

    reply = await force_invite.hand_out(_ctx(guild), wisteria, "Crrs", ("events",))

    assert "already here as an observer" in reply
    assert not await PendingInvite.exists()


# --------------------------------------------------------------------------
# Taking it back
# --------------------------------------------------------------------------


async def test_revoking_removes_the_invite(db, guild, wisteria, wynncraft_knows):
    ctx = _ctx(guild)
    await force_invite.hand_out(ctx, wisteria, "Crrs", ("events",))

    reply = await force_invite.take_back(ctx, wisteria)

    assert not await PendingInvite.exists()
    ctx.bot.http.delete_invite.assert_awaited()
    assert "revoked" in reply


async def test_revoking_refuses_to_touch_an_observer_invite(db, guild, wisteria):
    """Each command cancels the kind it mints, so a typo can't quietly
    cancel the other kind."""
    await PendingInvite.create(
        mc_uuid="uuid-wist",
        guild_tag="NONE",
        roles_bits=role_bits.OBSERVER,
        discord_invite_code="obs1",
    )

    reply = await force_invite.take_back(_ctx(guild), wisteria)

    assert "*observer* invite" in reply
    assert await PendingInvite.exists()


async def test_revoking_nothing_says_so(db, guild, wisteria):
    assert "no outstanding invite" in await force_invite.take_back(_ctx(guild), wisteria)


async def test_revoking_says_it_does_not_undo_a_used_invite(
    db, guild, wisteria, wynncraft_knows
):
    ctx = _ctx(guild)
    await force_invite.hand_out(ctx, wisteria, "Crrs", ("events",))

    reply = await force_invite.take_back(ctx, wisteria)

    assert "changes nothing" in reply
