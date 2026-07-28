"""The expel vote and the ban that follows it.

Three things are worth more than the rest here: the arithmetic at the
51% boundary (a float comparison gets 51-of-100 wrong), the electorate
being the guilds *seated* rather than every notable guild, and the ban
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
    NotabilityCache,
)
from hall_monitor.services import (
    delegate_registry,
    expel,
    expel_motion,
    notability,
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
def notable(monkeypatch):
    """Control which tags count as notable without touching the APIs."""
    tags = set()

    async def fake_is_notable(tag):
        return tag.upper() in tags

    monkeypatch.setattr(notability, "is_notable", fake_is_notable)
    return tags


async def seat(guild, notable, tag: str, user_id: int, *, uuid: str | None = None):
    """A guild with one representative present and in good standing."""
    notable.add(tag.upper())
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


async def test_the_electorate_is_the_guilds_seated_in_the_hall(db, guild, notable):
    """Not every notable guild: the cache knows dozens that have never
    sent anyone, and a 51% bar against those could never be cleared."""
    await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "ANO", 2)
    notable.add("NEVR")  # notable, but nobody from it has ever verified
    await NotabilityCache.create(
        guild_tag="NEVR", is_notable=True, signals_json="{}"
    )

    assert await expel_motion.electorate(guild) == {"vets", "ano"}


async def test_the_accused_guild_does_not_vote(db, guild, notable):
    await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "OTHR", 2)

    assert await expel_motion.electorate(guild, exclude="OTHR") == {"vets"}


async def test_one_guild_is_one_vote_however_many_representatives(
    db, guild, notable
):
    await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "VETS", 2, uuid="uuid-vets-2")

    assert await expel_motion.electorate(guild) == {"vets"}


async def test_a_relegated_guild_has_no_seat(db, guild, notable):
    """Losing notability takes the seat with it — a guild that isn't in
    the Hall doesn't get a say in who else is."""
    await seat(guild, notable, "VETS", 1)
    notable.discard("VETS")

    assert await expel_motion.electorate(guild) == set()


async def test_an_external_representative_has_no_seat(db, guild, notable):
    """Same line `services/contacts.py` draws for holding a slot: someone
    who has moved guilds isn't currently speaking for one here."""
    delegate = await seat(guild, notable, "VETS", 1)
    delegate.current_guild_tag = "ANO"
    await delegate.save()

    assert await expel_motion.electorate(guild) == set()


async def test_a_representative_who_left_the_server_has_no_seat(
    db, guild, notable
):
    await seat(guild, notable, "VETS", 1)
    guild.members.clear()

    assert await expel_motion.electorate(guild) == set()


async def test_a_forced_guild_seats_the_guild_it_points_at(db, guild, notable):
    """`~force guild` decides who someone represents, and the vote follows
    that like everything else does."""
    delegate = await seat(guild, notable, "VETS", 1)
    notable.add("ANO")
    await delegate_registry.set_forced_guild(delegate.discord_user_id, "ANO", None)

    assert await expel_motion.electorate(guild) == {"ano"}


# --------------------------------------------------------------------------
# Casting a vote
# --------------------------------------------------------------------------


async def test_a_vote_is_recorded_against_the_voters_guild(db, guild, notable):
    await seat(guild, notable, "VETS", 1)
    motion = await a_motion()

    outcome = await expel_motion.cast_vote(guild, motion, 1, yay=True)

    assert outcome.recorded
    row = await ExpelVote.get(motion_id=motion.id)
    assert row.guild_tag == "vets", "stored folded, so case can't split a vote"
    assert row.yay is True


async def test_a_second_representative_replaces_their_guilds_vote(
    db, guild, notable
):
    """One guild, one vote — and the second voter is told they've
    overridden a colleague rather than discovering it from the turnout."""
    await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "VETS", 2, uuid="uuid-vets-2")
    motion = await a_motion()

    await expel_motion.cast_vote(guild, motion, 1, yay=True)
    outcome = await expel_motion.cast_vote(guild, motion, 2, yay=False)

    assert outcome.recorded
    assert "replaces" in outcome.message
    assert await ExpelVote.filter(motion_id=motion.id).count() == 1
    assert (await ExpelVote.get(motion_id=motion.id)).yay is False


async def test_changing_your_own_vote_says_so(db, guild, notable):
    await seat(guild, notable, "VETS", 1)
    motion = await a_motion()

    await expel_motion.cast_vote(guild, motion, 1, yay=True)
    outcome = await expel_motion.cast_vote(guild, motion, 1, yay=False)

    assert "changed from your earlier vote" in outcome.message


async def test_a_non_delegate_cannot_vote(db, guild, notable):
    await seat(guild, notable, "VETS", 1)
    guild.add_member(99)  # in the server, but no Delegate row
    motion = await a_motion()

    outcome = await expel_motion.cast_vote(guild, motion, 99, yay=True)

    assert not outcome.recorded
    assert not await ExpelVote.exists()


async def test_the_accused_guilds_representatives_cannot_vote(db, guild, notable):
    await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "OTHR", 2)
    motion = await a_motion(target="OTHR")

    outcome = await expel_motion.cast_vote(guild, motion, 2, yay=False)

    assert not outcome.recorded
    assert "your own guild" in outcome.message


async def test_a_closed_motion_takes_no_more_votes(db, guild, notable):
    await seat(guild, notable, "VETS", 1)
    motion = await a_motion()
    motion.state = expel_motion.LAPSED
    await motion.save()

    outcome = await expel_motion.cast_vote(guild, motion, 1, yay=True)

    assert not outcome.recorded
    assert not await ExpelVote.exists()


async def test_a_vote_from_a_guild_that_has_left_stops_counting(
    db, guild, notable
):
    """The electorate moves under an open motion, so a guild that has lost
    its seat since voting shouldn't still be pushing the motion along."""
    await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "ANO", 2)
    motion = await a_motion()
    await expel_motion.cast_vote(guild, motion, 1, yay=True)

    notable.discard("VETS")
    voters = await expel_motion.electorate(guild, exclude="OTHR")

    assert (await expel_motion.tally(motion, voters)).yay == 0


# --------------------------------------------------------------------------
# Opening a motion
# --------------------------------------------------------------------------


async def test_a_non_delegate_cannot_move(db, guild, notable):
    await seat(guild, notable, "OTHR", 2)

    with pytest.raises(expel_motion.MotionRejected, match="on file"):
        await expel_motion.check_can_open(guild, None, "OTHR")


async def test_a_guild_cannot_move_to_expel_itself(db, guild, notable):
    mover = await seat(guild, notable, "VETS", 1)

    with pytest.raises(expel_motion.MotionRejected, match="itself"):
        await expel_motion.check_can_open(guild, mover, "VETS")


async def test_the_motion_records_the_halls_spelling_of_the_tag(
    db, guild, notable
):
    """`~expel_motion othr` and `~expel_motion OTHR` are one guild, and the
    post shouldn't shout back whatever case the mover happened to use."""
    mover = await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "OTHR", 2)

    assert await expel_motion.check_can_open(guild, mover, "othr") == "OTHR"


async def test_a_guild_that_is_not_seated_cannot_be_expelled(db, guild, notable):
    mover = await seat(guild, notable, "VETS", 1)

    with pytest.raises(expel_motion.MotionRejected, match="nothing to expel"):
        await expel_motion.check_can_open(guild, mover, "NEVR")


async def test_an_already_banned_guild_cannot_be_moved_against(
    db, guild, notable
):
    mover = await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "OTHR", 2)
    await ExpelBan.create(guild_tag="OTHR", reason="earlier")

    with pytest.raises(expel_motion.MotionRejected, match="already barred"):
        await expel_motion.check_can_open(guild, mover, "OTHR")


async def test_only_one_motion_per_guild_at_a_time(db, guild, notable):
    """Two open motions would split the vote and neither would carry."""
    mover = await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "OTHR", 2)
    await a_motion(target="OTHR")

    with pytest.raises(expel_motion.MotionRejected, match="already an open motion"):
        await expel_motion.check_can_open(guild, mover, "OTHR")


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


async def test_reaching_the_bar_expels_the_guild(db, guild, notable):
    await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "ANO", 2)
    target = await seat(guild, notable, "OTHR", 3)
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


async def test_short_of_the_bar_leaves_the_motion_open(db, guild, notable):
    await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "ANO", 2)
    await seat(guild, notable, "OTHR", 3)
    motion = await a_motion(target="OTHR")

    await expel_motion.cast_vote(guild, motion, 1, yay=True)

    assert await expel_motion.settle(guild, motion) is None
    assert not await expel.is_banned("OTHR")
    assert (await ExpelMotion.get(id=motion.id)).state == expel_motion.OPEN


async def test_abstention_counts_against(db, guild, notable):
    """Three seated voters, one yay, two silent — a motion carried by a
    single guild out of three doesn't remove anyone."""
    for index, tag in enumerate(("VETS", "ANO", "SEQ"), start=1):
        await seat(guild, notable, tag, index)
    await seat(guild, notable, "OTHR", 9)
    motion = await a_motion(target="OTHR")

    await expel_motion.cast_vote(guild, motion, 1, yay=True)

    assert await expel_motion.settle(guild, motion) is None


async def test_a_motion_lapses_at_its_deadline(db, guild, notable):
    await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "OTHR", 2)
    motion = await a_motion(target="OTHR")
    motion.created_at = datetime.now(timezone.utc) - timedelta(days=8)
    await motion.save()

    resolution = await expel_motion.settle(guild, motion)

    assert resolution is not None and resolution.state == expel_motion.LAPSED
    assert not await expel.is_banned("OTHR")
    guild.members[2].kick.assert_not_awaited()


async def test_the_bar_being_reached_on_the_last_day_still_carries(
    db, guild, notable
):
    """Passing is checked before the deadline, so a motion doesn't lapse
    on a technicality with the votes already in."""
    await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "OTHR", 2)
    motion = await a_motion(target="OTHR")
    motion.created_at = datetime.now(timezone.utc) - timedelta(days=8)
    await motion.save()
    await expel_motion.cast_vote(guild, motion, 1, yay=True)

    resolution = await expel_motion.settle(guild, motion)

    assert resolution is not None and resolution.state == expel_motion.PASSED


async def test_the_electorate_shrinking_can_carry_a_standing_vote(
    db, guild, notable
):
    """Nothing new is voted, but the Hall got smaller — which is why the
    hourly sweep resolves motions and not only the button does."""
    for index, tag in enumerate(("VETS", "ANO", "SEQ", "LOL"), start=1):
        await seat(guild, notable, tag, index)
    await seat(guild, notable, "OTHR", 9)
    motion = await a_motion(target="OTHR")
    await expel_motion.cast_vote(guild, motion, 1, yay=True)
    await expel_motion.cast_vote(guild, motion, 2, yay=True)
    assert await expel_motion.settle(guild, motion) is None, "two of four is short"

    notable.discard("SEQ")  # two of three now, which carries
    resolutions = await expel_motion.resolve_open(guild)

    assert [one.state for one in resolutions] == [expel_motion.PASSED]


async def test_the_recorded_tally_is_the_one_that_carried(db, guild, notable):
    """The electorate moves, so re-deriving the split a week later would
    answer with today's guilds rather than the ones who voted."""
    await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "OTHR", 2)
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


async def test_lifting_a_ban_restores_nothing_but_the_door(db, guild, notable):
    delegate = await seat(guild, notable, "OTHR", 1)
    await expel.expel(guild, "OTHR", reason="voted out")

    assert await expel.lift("OTHR")

    assert not await expel.is_banned("OTHR")
    assert (await Delegate.get(id=delegate.id)).left_at is not None, (
        "they still have to verify again"
    )


async def test_lifting_a_ban_that_isnt_there_says_so(db):
    assert await expel.lift("OTHR") is False


async def test_expelling_removes_whoever_represents_the_guild_now(
    db, guild, notable
):
    """By represented guild, not by the row — a `~force guild` repoint has
    to be caught, and someone repointed away must not be."""
    stayer = await seat(guild, notable, "OTHR", 1)
    await delegate_registry.set_forced_guild(1, "VETS", None)
    mover = await seat(guild, notable, "VETS", 2, uuid="uuid-2")
    await delegate_registry.set_forced_guild(2, "OTHR", None)

    await expel.expel(guild, "OTHR", reason="voted out")

    assert (await Delegate.get(id=stayer.id)).left_at is None
    guild.members[1].kick.assert_not_awaited()
    assert (await Delegate.get(id=mover.id)).left_at is not None
    guild.members[2].kick.assert_awaited()


async def test_a_kick_that_fails_still_leaves_the_guild_banned(
    db, guild, notable
):
    """The ban is what keeps them out; a rep still sitting in the server
    is visible and fixable, and the hourly sweep tries again."""
    import discord

    await seat(guild, notable, "OTHR", 1)
    guild.members[1].kick.side_effect = discord.HTTPException(
        MagicMock(status=403), "no"
    )

    removal = await expel.expel(guild, "OTHR", reason="voted out")

    assert await expel.is_banned("OTHR")
    assert removal.failed == [1]


async def test_the_hourly_sweep_re_removes_anyone_a_ban_missed(
    db, guild, notable
):
    """The backstop for a 403'd kick, a `~force guild` at a banned tag, or
    somebody who joined while the bot was down."""
    await ExpelBan.create(guild_tag="OTHR", reason="voted out")
    late = await seat(guild, notable, "OTHR", 1)

    removals = await expel.enforce(guild)

    assert [one.kicked for one in removals] == [[1]]
    assert (await Delegate.get(id=late.id)).left_at is not None


async def test_a_settled_hall_gives_the_sweep_nothing_to_do(db, guild, notable):
    await ExpelBan.create(guild_tag="OTHR", reason="voted out")
    await seat(guild, notable, "VETS", 1)

    assert await expel.enforce(guild) == []


# --------------------------------------------------------------------------
# Where the ban bites
# --------------------------------------------------------------------------


async def test_the_diagnostic_shows_turnout_but_not_the_split(db, guild, notable):
    """Anonymity a staff command opts out of isn't a property anyone can
    rely on — `~script motions` shows a monitor exactly what the channel
    shows everyone else, plus who is entitled to vote."""
    from hall_monitor.discord_bot.cogs.admin.scripts import motions

    await seat(guild, notable, "VETS", 1)
    await seat(guild, notable, "ANO", 2)
    await seat(guild, notable, "OTHR", 3)
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


async def test_the_roster_does_not_list_a_banned_guild(db):
    """Expulsion is about welcome, not significance — a guild the Hall
    voted out can be as notable as it ever was."""
    await NotabilityCache.create(
        guild_tag="OTHR", is_notable=True, signals_json="{}", guild_name="Others"
    )
    await NotabilityCache.create(
        guild_tag="VETS", is_notable=True, signals_json="{}", guild_name="Returners"
    )
    await ExpelBan.create(guild_tag="OTHR", reason="voted out")

    listed = await roster.listed_guilds()

    assert [one.tag for one in listed] == ["VETS"]
