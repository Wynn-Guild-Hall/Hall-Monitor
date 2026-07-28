"""The expel vote and the ban that follows it.

Three things are worth more than the rest here: the arithmetic at the
51% boundary (a float comparison gets 51-of-100 wrong), the electorate
being the guilds *seated* rather than every major guild, and the ban
actually holding at all four of the places a removed guild could get back
in.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from hall_monitor.db.models import (
    Delegate,
    ExpelBan,
    ExpelMotion,
    ExpelVote,
    GuildContact,
    MajorGuildCache,
)
from hall_monitor.services import (
    delegate_registry,
    expel,
    expel_motion,
    major_guilds,
    roster,
)


class FakeGuild:
    """Just enough ``discord.Guild`` to look members up and kick them."""

    def __init__(self):
        self.members: dict[int, MagicMock] = {}
        self.id = 1

    def get_member(self, user_id):
        return self.members.get(user_id)

    def add_member(self, user_id: int):
        member = MagicMock()
        member.id = user_id
        member.guild = self
        member.kick = AsyncMock()
        self.members[user_id] = member
        return member


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def major(monkeypatch):
    """Control which tags count as major without touching the APIs."""
    tags = set()

    async def fake_is_major(tag):
        return tag.upper() in tags

    monkeypatch.setattr(major_guilds, "is_major", fake_is_major)
    return tags


async def seat(guild, major, tag: str, user_id: int, *, uuid: str | None = None):
    """A guild with one representative present and in good standing."""
    major.add(tag.upper())
    guild.add_member(user_id)
    return await Delegate.create(
        mc_uuid=uuid or f"uuid-{user_id}",
        discord_user_id=user_id,
        guild_tag=tag,
        current_guild_tag=tag,
    )


async def a_motion(target: str = "OTHR", mover: str = "VETS") -> ExpelMotion:
    return await ExpelMotion.create(
        guild_tag=target, opened_by_discord_user_id=1, opened_by_guild_tag=mover
    )


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "electorate,yay,expected",
    [
        (100, 50, False),  # exactly half is not a majority
        (100, 51, True),  # the bar, exactly
        (100, 52, True),
        # 0.51 has no exact binary representation, so a float comparison
        # decides these by rounding error rather than by the rule.
        (200, 101, False),
        (200, 102, True),
        (3, 1, False),
        (3, 2, True),
        (2, 1, False),  # one of two is 50%
        (2, 2, True),
        (1, 1, True),
    ],
)
def test_the_threshold_is_51_percent_of_the_electorate(electorate, yay, expected):
    standing = expel_motion.Tally(electorate=electorate, yay=yay, nay=0)
    assert standing.passes is expected


def test_needed_agrees_with_passes_at_every_size():
    """`needed` is what the post tells voters the bar is. If it ever
    disagreed with the rule the vote is actually decided by, the motion
    would carry a vote early or late and the post would be lying."""
    for electorate in range(1, 200):
        needed = expel_motion.Tally(electorate=electorate, yay=0, nay=0).needed
        assert expel_motion.Tally(electorate, needed, 0).passes
        assert not expel_motion.Tally(electorate, needed - 1, 0).passes


def test_an_empty_hall_carries_nothing():
    """`needed` rounds to zero with no electorate, so without the guard a
    motion would pass on no votes at all."""
    assert not expel_motion.Tally(electorate=0, yay=0, nay=0).passes


# --------------------------------------------------------------------------
# Who votes
# --------------------------------------------------------------------------


async def test_the_electorate_is_the_guilds_seated_in_the_hall(db, guild, major):
    """Not every major guild: the cache knows dozens that have never
    sent anyone, and a 51% bar against those could never be cleared."""
    await seat(guild, major, "VETS", 1)
    await seat(guild, major, "ANO", 2)
    major.add("NEVR")  # major, but nobody from it has ever verified
    await MajorGuildCache.create(
        guild_tag="NEVR", is_major=True, signals_json="{}"
    )

    assert await expel_motion.electorate(guild) == {"vets", "ano"}


async def test_the_accused_guild_does_not_vote(db, guild, major):
    await seat(guild, major, "VETS", 1)
    await seat(guild, major, "OTHR", 2)

    assert await expel_motion.electorate(guild, exclude="OTHR") == {"vets"}


async def test_one_guild_is_one_vote_however_many_representatives(
    db, guild, major
):
    await seat(guild, major, "VETS", 1)
    await seat(guild, major, "VETS", 2, uuid="uuid-vets-2")

    assert await expel_motion.electorate(guild) == {"vets"}


async def test_a_relegated_guild_has_no_seat(db, guild, major):
    """Losing major-guild status takes the seat with it — a guild that isn't in
    the Hall doesn't get a say in who else is."""
    await seat(guild, major, "VETS", 1)
    major.discard("VETS")

    assert await expel_motion.electorate(guild) == set()


async def test_an_external_representative_has_no_seat(db, guild, major):
    """Same line `services/contacts.py` draws for holding a slot: someone
    who has moved guilds isn't currently speaking for one here."""
    delegate = await seat(guild, major, "VETS", 1)
    delegate.current_guild_tag = "ANO"
    await delegate.save()

    assert await expel_motion.electorate(guild) == set()


async def test_a_representative_who_left_the_server_has_no_seat(
    db, guild, major
):
    await seat(guild, major, "VETS", 1)
    guild.members.clear()

    assert await expel_motion.electorate(guild) == set()


async def test_a_forced_guild_seats_the_guild_it_points_at(db, guild, major):
    """`~force guild` decides who someone represents, and the vote follows
    that like everything else does."""
    delegate = await seat(guild, major, "VETS", 1)
    major.add("ANO")
    await delegate_registry.set_forced_guild(delegate.discord_user_id, "ANO", None)

    assert await expel_motion.electorate(guild) == {"ano"}


# --------------------------------------------------------------------------
# Casting a vote
# --------------------------------------------------------------------------


async def test_a_vote_is_recorded_against_the_voters_guild(db, guild, major):
    await seat(guild, major, "VETS", 1)
    motion = await a_motion()

    outcome = await expel_motion.cast_vote(guild, motion, 1, yay=True)

    assert outcome.recorded
    row = await ExpelVote.get(motion_id=motion.id)
    assert row.guild_tag == "vets", "stored folded, so case can't split a vote"
    assert row.yay is True


async def test_a_second_representative_replaces_their_guilds_vote(
    db, guild, major
):
    """One guild, one vote — and the second voter is told they've
    overridden a colleague rather than discovering it from the turnout."""
    await seat(guild, major, "VETS", 1)
    await seat(guild, major, "VETS", 2, uuid="uuid-vets-2")
    motion = await a_motion()

    await expel_motion.cast_vote(guild, motion, 1, yay=True)
    outcome = await expel_motion.cast_vote(guild, motion, 2, yay=False)

    assert outcome.recorded
    assert "replaces" in outcome.message
    assert await ExpelVote.filter(motion_id=motion.id).count() == 1
    assert (await ExpelVote.get(motion_id=motion.id)).yay is False


async def test_changing_your_own_vote_says_so(db, guild, major):
    await seat(guild, major, "VETS", 1)
    motion = await a_motion()

    await expel_motion.cast_vote(guild, motion, 1, yay=True)
    outcome = await expel_motion.cast_vote(guild, motion, 1, yay=False)

    assert "changed from your earlier vote" in outcome.message


async def test_a_non_delegate_cannot_vote(db, guild, major):
    await seat(guild, major, "VETS", 1)
    guild.add_member(99)  # in the server, but no Delegate row
    motion = await a_motion()

    outcome = await expel_motion.cast_vote(guild, motion, 99, yay=True)

    assert not outcome.recorded
    assert not await ExpelVote.exists()


async def test_the_accused_guilds_representatives_cannot_vote(db, guild, major):
    await seat(guild, major, "VETS", 1)
    await seat(guild, major, "OTHR", 2)
    motion = await a_motion(target="OTHR")

    outcome = await expel_motion.cast_vote(guild, motion, 2, yay=False)

    assert not outcome.recorded
    assert "your own guild" in outcome.message


async def test_a_closed_motion_takes_no_more_votes(db, guild, major):
    await seat(guild, major, "VETS", 1)
    motion = await a_motion()
    motion.state = expel_motion.LAPSED
    await motion.save()

    outcome = await expel_motion.cast_vote(guild, motion, 1, yay=True)

    assert not outcome.recorded
    assert not await ExpelVote.exists()


async def test_a_vote_from_a_guild_that_has_left_stops_counting(
    db, guild, major
):
    """The electorate moves under an open motion, so a guild that has lost
    its seat since voting shouldn't still be pushing the motion along."""
    await seat(guild, major, "VETS", 1)
    await seat(guild, major, "ANO", 2)
    motion = await a_motion()
    await expel_motion.cast_vote(guild, motion, 1, yay=True)

    major.discard("VETS")
    voters = await expel_motion.electorate(guild, exclude="OTHR")

    assert (await expel_motion.tally(motion, voters)).yay == 0


# --------------------------------------------------------------------------
# Opening a motion
# --------------------------------------------------------------------------


async def test_a_non_delegate_cannot_move(db, guild, major):
    await seat(guild, major, "OTHR", 2)

    with pytest.raises(expel_motion.MotionRejected, match="on file"):
        await expel_motion.check_can_open(guild, None, "OTHR")


async def test_a_guild_cannot_move_to_expel_itself(db, guild, major):
    mover = await seat(guild, major, "VETS", 1)

    with pytest.raises(expel_motion.MotionRejected, match="itself"):
        await expel_motion.check_can_open(guild, mover, "VETS")


async def test_the_motion_records_the_halls_spelling_of_the_tag(
    db, guild, major
):
    """`~expel_motion othr` and `~expel_motion OTHR` are one guild, and the
    post shouldn't shout back whatever case the mover happened to use."""
    mover = await seat(guild, major, "VETS", 1)
    await seat(guild, major, "OTHR", 2)

    assert await expel_motion.check_can_open(guild, mover, "othr") == "OTHR"


async def test_a_guild_that_is_not_seated_cannot_be_expelled(db, guild, major):
    mover = await seat(guild, major, "VETS", 1)

    with pytest.raises(expel_motion.MotionRejected, match="nothing to expel"):
        await expel_motion.check_can_open(guild, mover, "NEVR")


async def test_an_already_banned_guild_cannot_be_moved_against(
    db, guild, major
):
    mover = await seat(guild, major, "VETS", 1)
    await seat(guild, major, "OTHR", 2)
    await ExpelBan.create(guild_tag="OTHR", reason="earlier")

    with pytest.raises(expel_motion.MotionRejected, match="already barred"):
        await expel_motion.check_can_open(guild, mover, "OTHR")


async def test_only_one_motion_per_guild_at_a_time(db, guild, major):
    """Two open motions would split the vote and neither would carry."""
    mover = await seat(guild, major, "VETS", 1)
    await seat(guild, major, "OTHR", 2)
    await a_motion(target="OTHR")

    with pytest.raises(expel_motion.MotionRejected, match="already an open motion"):
        await expel_motion.check_can_open(guild, mover, "OTHR")


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


async def test_reaching_the_bar_expels_the_guild(db, guild, major):
    await seat(guild, major, "VETS", 1)
    await seat(guild, major, "ANO", 2)
    target = await seat(guild, major, "OTHR", 3)
    await GuildContact.create(guild_tag="OTHR", role="ownership", delegate=target)
    motion = await a_motion(target="OTHR")

    await expel_motion.cast_vote(guild, motion, 1, yay=True)
    await expel_motion.cast_vote(guild, motion, 2, yay=True)
    resolution = await expel_motion.settle(guild, motion)

    assert resolution is not None and resolution.state == expel_motion.PASSED
    assert await expel.is_banned("OTHR")
    guild.members[3].kick.assert_awaited()
    assert (await Delegate.get(id=target.id)).left_at is not None
    assert not await GuildContact.filter(guild_tag="OTHR").exists()


async def test_short_of_the_bar_leaves_the_motion_open(db, guild, major):
    await seat(guild, major, "VETS", 1)
    await seat(guild, major, "ANO", 2)
    await seat(guild, major, "OTHR", 3)
    motion = await a_motion(target="OTHR")

    await expel_motion.cast_vote(guild, motion, 1, yay=True)

    assert await expel_motion.settle(guild, motion) is None
    assert not await expel.is_banned("OTHR")
    assert (await ExpelMotion.get(id=motion.id)).state == expel_motion.OPEN


async def test_abstention_counts_against(db, guild, major):
    """Three seated voters, one yay, two silent — a motion carried by a
    single guild out of three doesn't remove anyone."""
    for index, tag in enumerate(("VETS", "ANO", "SEQ"), start=1):
        await seat(guild, major, tag, index)
    await seat(guild, major, "OTHR", 9)
    motion = await a_motion(target="OTHR")

    await expel_motion.cast_vote(guild, motion, 1, yay=True)

    assert await expel_motion.settle(guild, motion) is None


async def test_a_motion_lapses_at_its_deadline(db, guild, major):
    await seat(guild, major, "VETS", 1)
    await seat(guild, major, "OTHR", 2)
    motion = await a_motion(target="OTHR")
    motion.created_at = datetime.now(timezone.utc) - timedelta(days=8)
    await motion.save()

    resolution = await expel_motion.settle(guild, motion)

    assert resolution is not None and resolution.state == expel_motion.LAPSED
    assert not await expel.is_banned("OTHR")
    guild.members[2].kick.assert_not_awaited()


async def test_the_bar_being_reached_on_the_last_day_still_carries(
    db, guild, major
):
    """Passing is checked before the deadline, so a motion doesn't lapse
    on a technicality with the votes already in."""
    await seat(guild, major, "VETS", 1)
    await seat(guild, major, "OTHR", 2)
    motion = await a_motion(target="OTHR")
    motion.created_at = datetime.now(timezone.utc) - timedelta(days=8)
    await motion.save()
    await expel_motion.cast_vote(guild, motion, 1, yay=True)

    resolution = await expel_motion.settle(guild, motion)

    assert resolution is not None and resolution.state == expel_motion.PASSED


async def test_the_electorate_shrinking_can_carry_a_standing_vote(
    db, guild, major
):
    """Nothing new is voted, but the Hall got smaller — which is why the
    hourly sweep resolves motions and not only the button does."""
    for index, tag in enumerate(("VETS", "ANO", "SEQ", "LOL"), start=1):
        await seat(guild, major, tag, index)
    await seat(guild, major, "OTHR", 9)
    motion = await a_motion(target="OTHR")
    await expel_motion.cast_vote(guild, motion, 1, yay=True)
    await expel_motion.cast_vote(guild, motion, 2, yay=True)
    assert await expel_motion.settle(guild, motion) is None, "two of four is short"

    major.discard("SEQ")  # two of three now, which carries
    resolutions = await expel_motion.resolve_open(guild)

    assert [one.state for one in resolutions] == [expel_motion.PASSED]


async def test_the_recorded_tally_is_the_one_that_carried(db, guild, major):
    """The electorate moves, so re-deriving the split a week later would
    answer with today's guilds rather than the ones who voted."""
    await seat(guild, major, "VETS", 1)
    await seat(guild, major, "OTHR", 2)
    motion = await a_motion(target="OTHR")
    await expel_motion.cast_vote(guild, motion, 1, yay=True)

    resolution = await expel_motion.settle(guild, motion)

    assert resolution.tally.as_dict() == {
        "electorate": 1,
        "yay": 1,
        "nay": 0,
        "needed": 1,
    }
    assert (await ExpelMotion.get(id=motion.id)).tally_json


# --------------------------------------------------------------------------
# Anonymity
# --------------------------------------------------------------------------


def test_the_live_post_cannot_distinguish_one_split_from_another():
    """A running yay counter beside a member list is enough to infer
    individual votes, which would make the anonymity decorative. The
    property is stronger than "the number isn't printed": two motions
    with the same turnout and opposite splits have to render identically,
    or something in the wording is leaking the difference.
    """
    motion = ExpelMotion(
        guild_tag="OTHR",
        opened_by_discord_user_id=1,
        opened_by_guild_tag="VETS",
        created_at=datetime.now(timezone.utc),
    )

    mostly_yay = expel_motion.render_open(
        motion, expel_motion.Tally(electorate=20, yay=7, nay=2), None
    )
    mostly_nay = expel_motion.render_open(
        motion, expel_motion.Tally(electorate=20, yay=2, nay=7), None
    )

    assert mostly_yay == mostly_nay
    assert "**9** of **20**" in mostly_yay, "turnout itself is shown"


def test_nothing_rendered_names_the_mover():
    """A named mover turns "the Hall is considering this" into "these
    people came for you", which is why the command is DM-only."""
    motion = ExpelMotion(
        guild_tag="OTHR",
        opened_by_discord_user_id=4242,
        opened_by_guild_tag="VETS",
        created_at=datetime.now(timezone.utc),
    )
    standing = expel_motion.Tally(electorate=20, yay=3, nay=1)

    for body in (
        expel_motion.render_open(motion, standing, "Others"),
        expel_motion.render_call(motion, standing, "Others"),
        expel_motion.render_resolved(motion, standing, "Others"),
    ):
        assert "VETS" not in body
        assert "4242" not in body


def test_the_call_to_the_hall_does_not_leak_the_tally():
    """The trigger is public and the count equals it in every case but a
    shrinking electorate, so printing the count buys nothing."""
    motion = ExpelMotion(
        guild_tag="OTHR",
        opened_by_discord_user_id=1,
        opened_by_guild_tag="VETS",
        created_at=datetime.now(timezone.utc),
    )

    at_the_line = expel_motion.render_call(
        motion, expel_motion.Tally(electorate=20, yay=3, nay=0), None
    )
    well_past_it = expel_motion.render_call(
        motion, expel_motion.Tally(electorate=20, yay=9, nay=4), None
    )

    assert at_the_line == well_past_it
    assert "@here" in at_the_line


def test_the_result_bar_names_nobody():
    """`guild_bar.attributed` exists and would be clearer here — each
    guild's banner over its own square. It also publishes a permanent
    record of who voted to remove whom, which is the thing this vote is
    designed to avoid, so the bar is sorted by outcome instead."""
    from hall_monitor.services import guild_bar

    standing = expel_motion.Tally(electorate=10, yay=5, nay=3)

    bar = expel_motion.render_bar(standing)

    assert bar == guild_bar.GREEN * 5 + guild_bar.WHITE * 2 + guild_bar.RED * 3
    assert ":" not in bar, "no custom emote, so no guild is identifiable"
    # Same counts, entirely different voters, identical row.
    assert bar == expel_motion.render_bar(
        expel_motion.Tally(electorate=10, yay=5, nay=3)
    )


def test_the_bar_appears_only_once_the_motion_is_resolved():
    """A bar that grew as votes arrived would let anyone watching match a
    new square to whoever was online — which is the leak the turnout-only
    rule exists to close."""
    from hall_monitor.services import guild_bar

    motion = ExpelMotion(
        guild_tag="OTHR",
        opened_by_discord_user_id=1,
        opened_by_guild_tag="VETS",
        created_at=datetime.now(timezone.utc),
    )
    standing = expel_motion.Tally(electorate=10, yay=5, nay=3)

    assert guild_bar.GREEN not in expel_motion.render_open(motion, standing, None)
    assert guild_bar.GREEN in expel_motion.render_resolved(motion, standing, None)


def test_the_split_is_published_once_the_motion_closes():
    motion = ExpelMotion(
        guild_tag="OTHR",
        opened_by_discord_user_id=1,
        opened_by_guild_tag="VETS",
        state=expel_motion.PASSED,
        created_at=datetime.now(timezone.utc),
    )
    standing = expel_motion.Tally(electorate=20, yay=11, nay=2)

    body = expel_motion.render_resolved(motion, standing, "Others")

    assert "11" in body and "2" in body


# --------------------------------------------------------------------------
# Keeping the mover out of sight
# --------------------------------------------------------------------------


def _public_ctx():
    """A ``~expel_motion`` run in a channel where everyone can read it."""
    ctx = MagicMock()
    ctx.guild = MagicMock()
    ctx.author.id = 42
    ctx.author.send = AsyncMock()
    ctx.message.delete = AsyncMock()
    ctx.send = AsyncMock()
    ctx.reply = AsyncMock()
    return ctx


async def test_a_public_invocation_is_deleted_and_answered_privately(db):
    """The message itself is the leak, and no care inside the bot takes it
    back — so it goes, and the explanation is DM'd rather than posted
    under it where it would only draw eyes."""
    from hall_monitor.discord_bot.cogs.moderation import expel as cog

    ctx = _public_ctx()
    await cog.Expel(MagicMock())._redirect_to_dm(ctx)

    ctx.message.delete.assert_awaited()
    ctx.reply.assert_not_awaited()
    ctx.send.assert_not_awaited()
    assert "anonymous" in ctx.author.send.await_args.args[0]


async def test_a_public_invocation_we_cant_delete_says_so(db):
    """Without Manage Messages the mover has to do it themselves, and
    being told nothing would leave them thinking they were covered."""
    import discord

    from hall_monitor.discord_bot.cogs.moderation import expel as cog

    ctx = _public_ctx()
    ctx.message.delete.side_effect = discord.Forbidden(MagicMock(status=403), "no")

    await cog.Expel(MagicMock())._redirect_to_dm(ctx)

    assert "still visible" in ctx.author.send.await_args.args[0]


# --------------------------------------------------------------------------
# Calling the Hall to a motion
# --------------------------------------------------------------------------


@pytest.fixture
def delegate_channel(monkeypatch):
    """A stand-in for delegate general, with `DELEGATE_CHANNEL_ID` set."""
    channel = MagicMock()
    channel.id = 777
    channel.send = AsyncMock()
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.moderation.expel.settings"
        ".delegate_channel_id",
        777,
    )
    return channel


def _wire(guild, channel):
    guild.get_channel = lambda channel_id: (
        channel if channel_id == channel.id else None
    )


async def test_one_guild_alone_never_pings_the_server(
    db, guild, major, delegate_channel
):
    """The whole point. People leave servers over stray pings, and a
    motion nobody else has backed is one member's opinion."""
    from hall_monitor.discord_bot.cogs.moderation import expel as cog

    _wire(guild, delegate_channel)
    for index, tag in enumerate(("VETS", "ANO", "SEQ", "LOL", "FIVE"), start=1):
        await seat(guild, major, tag, index)
    await seat(guild, major, "OTHR", 9)
    motion = await a_motion(target="OTHR")
    await expel_motion.cast_vote(guild, motion, 1, yay=True)

    assert await cog.announce_if_ready(MagicMock(), guild, motion) is False
    delegate_channel.send.assert_not_awaited()


async def test_three_guilds_in_favour_calls_the_hall_once(
    db, guild, major, delegate_channel
):
    from hall_monitor.discord_bot.cogs.moderation import expel as cog

    _wire(guild, delegate_channel)
    for index, tag in enumerate(("VETS", "ANO", "SEQ", "LOL", "FIVE"), start=1):
        await seat(guild, major, tag, index)
    await seat(guild, major, "OTHR", 9)
    motion = await a_motion(target="OTHR")
    for voter in (1, 2, 3):
        await expel_motion.cast_vote(guild, motion, voter, yay=True)

    assert await cog.announce_if_ready(MagicMock(), guild, motion) is True

    body = delegate_channel.send.await_args.args[0]
    assert "@here" in body
    mentions = delegate_channel.send.await_args.kwargs["allowed_mentions"]
    assert mentions.everyone is True, "otherwise it renders as plain text"
    assert (await ExpelMotion.get(id=motion.id)).announced_at is not None

    # A fourth yay must not ping the room a second time.
    await expel_motion.cast_vote(guild, motion, 4, yay=True)
    assert await cog.announce_if_ready(MagicMock(), guild, motion) is False
    assert delegate_channel.send.await_count == 1


async def test_a_motion_that_has_already_carried_calls_nobody(
    db, guild, major, delegate_channel
):
    """Nothing left to rally to — and the guild is already gone."""
    from hall_monitor.discord_bot.cogs.moderation import expel as cog

    _wire(guild, delegate_channel)
    for index, tag in enumerate(("VETS", "ANO", "SEQ"), start=1):
        await seat(guild, major, tag, index)
    await seat(guild, major, "OTHR", 9)
    motion = await a_motion(target="OTHR")
    for voter in (1, 2, 3):
        await expel_motion.cast_vote(guild, motion, voter, yay=True)
    await expel_motion.settle(guild, motion)

    assert await cog.announce_if_ready(MagicMock(), guild, motion) is False
    delegate_channel.send.assert_not_awaited()


async def test_an_unset_delegate_channel_doesnt_stop_the_vote(
    db, guild, major, monkeypatch
):
    """Nobody is called to it, and it still runs and still resolves."""
    from hall_monitor.discord_bot.cogs.moderation import expel as cog

    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.moderation.expel.settings"
        ".delegate_channel_id",
        0,
    )
    for index, tag in enumerate(("VETS", "ANO", "SEQ", "LOL", "FIVE"), start=1):
        await seat(guild, major, tag, index)
    await seat(guild, major, "OTHR", 9)
    motion = await a_motion(target="OTHR")
    for voter in (1, 2, 3):
        await expel_motion.cast_vote(guild, motion, voter, yay=True)

    assert await cog.announce_if_ready(MagicMock(), guild, motion) is False
    assert (await ExpelMotion.get(id=motion.id)).announced_at is None
    assert (await ExpelMotion.get(id=motion.id)).state == expel_motion.OPEN


async def test_a_failed_call_is_retried_rather_than_spent(
    db, guild, major, delegate_channel
):
    """The row is stamped only once the message is out, so a motion never
    silently loses the single announcement it gets."""
    import discord

    from hall_monitor.discord_bot.cogs.moderation import expel as cog

    _wire(guild, delegate_channel)
    for index, tag in enumerate(("VETS", "ANO", "SEQ", "LOL", "FIVE"), start=1):
        await seat(guild, major, tag, index)
    await seat(guild, major, "OTHR", 9)
    motion = await a_motion(target="OTHR")
    for voter in (1, 2, 3):
        await expel_motion.cast_vote(guild, motion, voter, yay=True)
    delegate_channel.send.side_effect = discord.HTTPException(
        MagicMock(status=500), "nope"
    )

    assert await cog.announce_if_ready(MagicMock(), guild, motion) is False
    assert (await ExpelMotion.get(id=motion.id)).announced_at is None

    delegate_channel.send.side_effect = None
    assert await cog.announce_if_ready(MagicMock(), guild, motion) is True


# --------------------------------------------------------------------------
# The ban
# --------------------------------------------------------------------------


async def test_a_ban_folds_case(db):
    await expel.record_ban("OTHR", reason="voted out")

    assert await expel.is_banned("othr")
    assert await expel.banned_tags() == {"othr"}


async def test_re_banning_updates_rather_than_duplicating(db):
    """Two rows would mean one `~unforce expel` left the guild banned."""
    await expel.record_ban("OTHR", reason="first")
    await expel.record_ban("othr", reason="second")

    assert await ExpelBan.all().count() == 1
    assert (await ExpelBan.first()).reason == "second"


async def test_lifting_a_ban_restores_nothing_but_the_door(db, guild, major):
    delegate = await seat(guild, major, "OTHR", 1)
    await expel.expel(guild, "OTHR", reason="voted out")

    assert await expel.lift("OTHR")

    assert not await expel.is_banned("OTHR")
    assert (await Delegate.get(id=delegate.id)).left_at is not None, (
        "they still have to verify again"
    )


async def test_lifting_a_ban_that_isnt_there_says_so(db):
    assert await expel.lift("OTHR") is False


async def test_expelling_removes_whoever_represents_the_guild_now(
    db, guild, major
):
    """By represented guild, not by the row — a `~force guild` repoint has
    to be caught, and someone repointed away must not be."""
    stayer = await seat(guild, major, "OTHR", 1)
    await delegate_registry.set_forced_guild(1, "VETS", None)
    mover = await seat(guild, major, "VETS", 2, uuid="uuid-2")
    await delegate_registry.set_forced_guild(2, "OTHR", None)

    await expel.expel(guild, "OTHR", reason="voted out")

    assert (await Delegate.get(id=stayer.id)).left_at is None
    guild.members[1].kick.assert_not_awaited()
    assert (await Delegate.get(id=mover.id)).left_at is not None
    guild.members[2].kick.assert_awaited()


async def test_a_kick_that_fails_still_leaves_the_guild_banned(
    db, guild, major
):
    """The ban is what keeps them out; a rep still sitting in the server
    is visible and fixable, and the hourly sweep tries again."""
    import discord

    await seat(guild, major, "OTHR", 1)
    guild.members[1].kick.side_effect = discord.HTTPException(
        MagicMock(status=403), "no"
    )

    removal = await expel.expel(guild, "OTHR", reason="voted out")

    assert await expel.is_banned("OTHR")
    assert removal.failed == [1]


async def test_the_hourly_sweep_re_removes_anyone_a_ban_missed(
    db, guild, major
):
    """The backstop for a 403'd kick, a `~force guild` at a banned tag, or
    somebody who joined while the bot was down."""
    await ExpelBan.create(guild_tag="OTHR", reason="voted out")
    late = await seat(guild, major, "OTHR", 1)

    removals = await expel.enforce(guild)

    assert [one.kicked for one in removals] == [[1]]
    assert (await Delegate.get(id=late.id)).left_at is not None


async def test_a_settled_hall_gives_the_sweep_nothing_to_do(db, guild, major):
    await ExpelBan.create(guild_tag="OTHR", reason="voted out")
    await seat(guild, major, "VETS", 1)

    assert await expel.enforce(guild) == []


# --------------------------------------------------------------------------
# Where the ban bites
# --------------------------------------------------------------------------


async def test_the_diagnostic_shows_turnout_but_not_the_split(db, guild, major):
    """Anonymity a staff command opts out of isn't a property anyone can
    rely on — `~script motions` shows a monitor exactly what the channel
    shows everyone else, plus who is entitled to vote."""
    from hall_monitor.discord_bot.cogs.admin.scripts import motions

    await seat(guild, major, "VETS", 1)
    await seat(guild, major, "ANO", 2)
    await seat(guild, major, "OTHR", 3)
    motion = await a_motion(target="OTHR")
    await expel_motion.cast_vote(guild, motion, 1, yay=True)

    ctx = MagicMock()
    ctx.guild = guild
    ctx.reply = AsyncMock()
    await motions.main(ctx)

    body = ctx.reply.await_args.args[0]
    assert "`VETS`" in body and "`ANO`" in body, "the electorate is named"
    assert "1 of 2 voted" in body
    assert "yay ·" not in body and "nay" not in body
    assert "moved by" not in body.lower(), "the mover is anonymous here too"


async def test_the_roster_does_not_list_a_banned_guild(db):
    """Expulsion is about welcome, not significance — a guild the Hall
    voted out can be as major as it ever was."""
    await MajorGuildCache.create(
        guild_tag="OTHR", is_major=True, signals_json="{}", guild_name="Others"
    )
    await MajorGuildCache.create(
        guild_tag="VETS", is_major=True, signals_json="{}", guild_name="Returners"
    )
    await ExpelBan.create(guild_tag="OTHR", reason="voted out")

    listed = await roster.listed_guilds()

    assert [one.tag for one in listed] == ["VETS"]
