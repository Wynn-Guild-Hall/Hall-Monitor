"""The expel vote — who may vote, what carries it, and when it ends.

A delegate moves that a guild be removed from the Hall; every other guild
seated in the Hall gets one vote; the motion passes at **51%** yay. What
follows from that sentence, and none of it is obvious:

**Guilds vote, not people.** A guild with three representatives is still
one guild, so ``ExpelVote`` is unique per ``(motion, guild)`` and a later
vote from any of its representatives replaces the earlier one. The ballot
says so when it happens — a representative discovering by accident that a
colleague overrode them is worse than being told.

**The electorate is who's in the room.** Not every guild the notability
cache marks notable: with 43 notable guilds and five of them ever having
sent a representative, a 51% bar against the whole set could not be
cleared by unanimity. So it's the guilds *seated* — at least one live
representative, in the server, wearing the Delegate standing. A relegate
or an external rep doesn't vote, for the same reason they don't hold a
contact slot: they aren't currently speaking for a guild in the Hall.

**Abstention counts against.** The bar is 51% of the electorate, not of
those who turn out, so a motion carried by three guilds out of twenty
doesn't pass. Removing a guild should need the Hall to actually want it.

**The accused guild doesn't vote**, and isn't in the denominator either.
Sitting on the jury for your own trial is one thing; leaving them in the
denominator as a guaranteed non-yay is a quieter version of the same
problem, and it silently raises the bar for everyone else.

**The electorate moves under the motion.** Guilds gain and lose
notability, verify, and leave, all while a vote is open — so the tally is
recomputed from the current electorate every time, and a vote from a
guild that has since left the Hall stops counting. Two consequences worth
naming:

- A motion can pass *without a new vote*, if the electorate shrinks under
  a standing yay count. That's why resolution is also checked on the
  hourly sweep and not only when somebody presses a button.
- There is deliberately **no early failure**. "This can no longer pass"
  isn't a stable fact when the denominator can shrink, so a motion ends
  by passing or by lapsing at its deadline — never by being declared dead
  while it might still recover.

**The bot never says who voted what — or who moved it.** The live post
carries turnout and the bar, and the split is published only once the
motion is resolved. Nothing anywhere names the mover or their guild.
That isn't squeamishness: a running yay counter next to a member list is
enough to infer individual votes, and a named mover turns "the Hall is
considering this" into "these people came for you". The command is run
by DM for exactly that reason. Note that anonymity is a property of what
the bot *renders* — the rows record who cast and moved what, because
something unauditable is a worse problem than something unpublished.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from hall_monitor.config import settings
from hall_monitor.db.models import (
    Delegate,
    ExpelMotion,
    ExpelVote,
    NotabilityCache,
)
from hall_monitor.services import (
    delegate_registry,
    expel,
    guild_bar,
    guild_tag as tags,
    notability,
)

logger = logging.getLogger(__name__)

OPEN = "open"
PASSED = "passed"
LAPSED = "lapsed"

# The bar, as a whole-number percentage. Integer arithmetic throughout:
# 0.51 has no exact binary representation, so a float comparison decides
# the 51-of-100 case by rounding error rather than by the rule.
THRESHOLD_PERCENT = 51

# How many guilds have to be behind a motion before the Hall is called to
# it with an `@here`. One person deciding to notify everybody is not a
# thing this bot does — people leave servers over stray pings, and a
# motion nobody else supports has no claim on anyone's attention. Three
# guilds is enough to say the Hall is actually considering it.
ANNOUNCE_AT_YAY = 3


class MotionRejected(Exception):
    """Carries the user-facing reason a motion can't be opened."""


@dataclass(frozen=True)
class Tally:
    """Where a motion stands against the current electorate."""

    electorate: int
    yay: int
    nay: int

    @property
    def needed(self) -> int:
        """Yay votes required to carry, rounded up."""
        return -(-THRESHOLD_PERCENT * self.electorate // 100)

    @property
    def voted(self) -> int:
        return self.yay + self.nay

    @property
    def passes(self) -> bool:
        # An empty Hall carries nothing. Without this guard `needed` is
        # zero and a motion with no electorate would pass on no votes at
        # all, which is the one outcome nobody could argue for.
        if self.electorate <= 0:
            return False
        return self.yay * 100 >= THRESHOLD_PERCENT * self.electorate

    def as_dict(self) -> dict[str, int]:
        return {
            "electorate": self.electorate,
            "yay": self.yay,
            "nay": self.nay,
            "needed": self.needed,
        }


# --------------------------------------------------------------------------
# The electorate
# --------------------------------------------------------------------------


async def seated_guilds(discord_guild: discord.Guild) -> dict[str, str]:
    """Comparison key → display tag for every guild seated in the Hall.

    Seated means: at least one ``Delegate`` row that hasn't left, whose
    member is still in the server, and whose standing works out to
    ``delegate`` — so a relegated guild and an external representative
    both fall out, which is the same line ``services/contacts.py`` draws
    for holding a slot.

    Notability is memoised per tag across the walk: it's a cached read,
    but a Hall of fifty representatives across twenty guilds would
    otherwise ask the same question fifty times.
    """
    seated: dict[str, str] = {}
    notable: dict[str, bool] = {}
    for delegate in await Delegate.filter(left_at=None):
        if discord_guild.get_member(delegate.discord_user_id) is None:
            continue
        tag = await delegate_registry.represented_guild(delegate)
        key = tags.normalise(tag)
        if key in seated:
            continue  # already seated by one of their colleagues
        if key not in notable:
            notable[key] = await notability.is_notable(tag)
        standing = await delegate_registry.standing(delegate, notable=notable[key])
        if standing != delegate_registry.DELEGATE:
            continue
        seated[key] = tag
    return seated


async def electorate(
    discord_guild: discord.Guild, *, exclude: str | None = None
) -> set[str]:
    """Comparison keys of the guilds entitled to vote on a motion.

    Every seated guild bar the one on trial.
    """
    keys = set(await seated_guilds(discord_guild))
    if exclude is not None:
        keys.discard(tags.normalise(exclude))
    return keys


async def tally(motion: ExpelMotion, voters: set[str]) -> Tally:
    """Count ``motion``'s votes against the guilds currently entitled to vote.

    Votes from guilds no longer in the electorate are dropped rather than
    carried forward: a guild that has left the Hall or lost its standing
    since voting shouldn't still be pushing a motion along behind it.
    """
    rows = await ExpelVote.filter(motion_id=motion.id).values("guild_tag", "yay")
    counted = [row for row in rows if row["guild_tag"] in voters]
    yay = sum(1 for row in counted if row["yay"])
    return Tally(electorate=len(voters), yay=yay, nay=len(counted) - yay)


# --------------------------------------------------------------------------
# Opening a motion
# --------------------------------------------------------------------------


async def check_can_open(
    discord_guild: discord.Guild, mover: Delegate | None, guild_tag: str
) -> str:
    """Everything that has to be true before a motion is worth posting.

    Returns the mover's guild tag; raises :class:`MotionRejected` with the
    message the caller should see. Checked up front rather than at
    resolution because a motion that could never have carried still costs
    every delegate in the Hall a notification.
    """
    if mover is None or mover.left_at is not None:
        raise MotionRejected(
            "I don't have you on file as a representative — only a guild's "
            "delegates can move to expel one."
        )
    moving_for = await delegate_registry.represented_guild(mover)
    if tags.matches(moving_for, guild_tag):
        raise MotionRejected(
            f"you represent `{moving_for}` — a guild can't move to expel itself."
        )
    if await expel.is_banned(guild_tag):
        raise MotionRejected(f"`{guild_tag}` is already barred from the Hall.")

    seated = await seated_guilds(discord_guild)
    if tags.normalise(moving_for) not in seated:
        raise MotionRejected(
            f"`{moving_for}` isn't currently seated in the Hall, so it has no "
            "motion to make. That's usually a guild that's dropped out of "
            "notability, or a representative who has moved guilds."
        )
    if tags.normalise(guild_tag) not in seated:
        raise MotionRejected(
            f"`{guild_tag}` isn't seated in the Hall — there's nothing to expel."
        )
    if await ExpelMotion.filter(guild_tag__iexact=guild_tag, state=OPEN).exists():
        raise MotionRejected(
            f"there's already an open motion against `{guild_tag}`. Two would "
            "split the vote between them and neither would reach the bar."
        )
    return seated[tags.normalise(guild_tag)]


async def open_motion(
    mover: Delegate, moving_for: str, guild_tag: str
) -> ExpelMotion:
    """Record the motion. Posting it is the cog's job."""
    return await ExpelMotion.create(
        guild_tag=guild_tag,
        opened_by_discord_user_id=mover.discord_user_id,
        opened_by_guild_tag=moving_for,
    )


# --------------------------------------------------------------------------
# Voting
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VoteOutcome:
    """What casting a vote did, phrased for the voter who cast it."""

    recorded: bool
    message: str
    tally: Tally | None = None


async def cast_vote(
    discord_guild: discord.Guild,
    motion: ExpelMotion,
    voter_id: int,
    *,
    yay: bool,
) -> VoteOutcome:
    """Record one guild's vote, replacing whatever it had before.

    Every rejection says which rule stopped it. A ballot button that
    simply doesn't respond is indistinguishable from a bot that's down,
    and the voter has no way to find out which.
    """
    if motion.state != OPEN:
        return VoteOutcome(False, "that vote has closed.")

    voter = await delegate_registry.get_by_discord_user_id(voter_id)
    if voter is None or voter.left_at is not None:
        return VoteOutcome(
            False,
            "only guild representatives vote here — I don't have you on file "
            "as one.",
        )
    voting_for = await delegate_registry.represented_guild(voter)
    key = tags.normalise(voting_for)

    if tags.matches(voting_for, motion.guild_tag):
        return VoteOutcome(
            False, "this motion is about your own guild, so you don't vote on it."
        )

    voters = await electorate(discord_guild, exclude=motion.guild_tag)
    if key not in voters:
        return VoteOutcome(
            False,
            f"`{voting_for}` isn't seated in the Hall right now, so it has no "
            "vote on this.",
        )

    previous = await ExpelVote.filter(motion_id=motion.id, guild_tag=key).first()
    changed_by_other = (
        previous is not None and previous.discord_user_id != voter_id
    )
    await ExpelVote.update_or_create(
        motion_id=motion.id,
        guild_tag=key,
        defaults={"discord_user_id": voter_id, "yay": yay},
    )

    choice = "yay" if yay else "nay"
    if previous is None:
        note = f"`{voting_for}` votes **{choice}**."
    elif previous.yay == yay and not changed_by_other:
        note = f"`{voting_for}` was already voting **{choice}**."
    elif changed_by_other:
        note = (
            f"`{voting_for}` now votes **{choice}** — that replaces the vote "
            "another of your guild's representatives had cast. One guild, one "
            "vote."
        )
    else:
        note = f"`{voting_for}` now votes **{choice}**, changed from your earlier vote."

    return VoteOutcome(True, note, await tally(motion, voters))


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def deadline(motion: ExpelMotion) -> datetime:
    return motion.created_at + timedelta(days=settings.expel_motion_days)


def has_lapsed(motion: ExpelMotion) -> bool:
    return deadline(motion) <= datetime.now(timezone.utc)


@dataclass(frozen=True)
class Resolution:
    """A motion that ended, and what ending it cost."""

    motion: ExpelMotion
    state: str
    tally: Tally
    removal: expel.Removal | None = None

    def line(self) -> str:
        detail = (
            f"{self.tally.yay} yay / {self.tally.nay} nay of "
            f"{self.tally.electorate} guild(s), needed {self.tally.needed}"
        )
        return f"motion against {self.motion.guild_tag} {self.state} — {detail}"


async def settle(
    discord_guild: discord.Guild, motion: ExpelMotion
) -> Resolution | None:
    """Pass, lapse, or leave a motion open. Returns ``None`` for the last.

    Passing is checked before the deadline, so a motion that reaches the
    bar on its final day carries rather than lapsing on a technicality.
    """
    if motion.state != OPEN:
        return None
    voters = await electorate(discord_guild, exclude=motion.guild_tag)
    standing = await tally(motion, voters)

    if standing.passes:
        removal = await expel.expel(
            discord_guild,
            motion.guild_tag,
            reason=(
                f"hall-monitor: expelled by delegate vote "
                f"({standing.yay}/{standing.electorate})"
            ),
            motion=motion,
        )
        return await _close(motion, PASSED, standing, removal)
    if has_lapsed(motion):
        return await _close(motion, LAPSED, standing, None)
    return None


async def resolve_open(discord_guild: discord.Guild) -> list[Resolution]:
    """Settle every open motion. Runs on the hourly sweep and after a vote.

    The hourly pass isn't only about deadlines: the electorate moves on
    its own, so a motion can reach the bar with no new vote behind it and
    nothing would otherwise notice.
    """
    resolutions = []
    for motion in await ExpelMotion.filter(state=OPEN):
        try:
            resolution = await settle(discord_guild, motion)
        except Exception:  # noqa: BLE001 — one motion must not stop the rest
            logger.exception(
                "expel: couldn't settle the motion against %s", motion.guild_tag
            )
            continue
        if resolution is not None:
            logger.info("expel: %s", resolution.line())
            resolutions.append(resolution)
    return resolutions


async def _close(
    motion: ExpelMotion,
    state: str,
    standing: Tally,
    removal: expel.Removal | None,
) -> Resolution:
    motion.state = state
    motion.tally_json = json.dumps(standing.as_dict())
    motion.resolved_at = datetime.now(timezone.utc)
    await motion.save(update_fields=["state", "tally_json", "resolved_at"])
    return Resolution(motion=motion, state=state, tally=standing, removal=removal)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


async def guild_name(guild_tag: str) -> str | None:
    """Wynncraft's spelling of a guild's full name, if the sweep learned it."""
    row = await NotabilityCache.filter(guild_tag__iexact=guild_tag).first()
    return row.guild_name if row is not None else None


def render_bar(standing: Tally) -> str:
    """The result as one row of squares: yay, then silent, then nay.

    **Deliberately not `guild_bar.attributed`.** That function exists,
    and it would be genuinely clearer here — each guild's banner over its
    own square, readable guild by guild. It also publishes exactly what
    the Hall is built to avoid: a durable, screenshottable record of who
    voted to remove whom. The point of this vote is that a guild can go
    without anyone being left with somebody to blame, and a bar with
    names on it hands out that grudge in a form that outlives the
    argument.

    So the squares are sorted by outcome and carry no identity at all.
    Two motions with the same counts and entirely opposite voters render
    the same row. Only ever shown on a *resolved* motion — a bar that
    updated as votes arrived would let anyone watching correlate a new
    square with whoever was just online.
    """
    silent = max(0, standing.electorate - standing.voted)
    return guild_bar.row(
        guild_bar.GREEN * standing.yay
        + guild_bar.WHITE * silent
        + guild_bar.RED * standing.nay
    )


def should_announce(motion: ExpelMotion, standing: Tally) -> bool:
    """Whether this motion has earned the one `@here` it may ever get.

    Three conditions, and the second two matter as much as the first: it
    must still be **open** (a carried motion needs no audience, and a
    lapsed one has none), and it must not have been announced **already**
    — hence the column, since "three guilds are behind it" stays true for
    the rest of the vote and would otherwise fire on every pass.
    """
    return (
        motion.state == OPEN
        and motion.announced_at is None
        and standing.yay >= ANNOUNCE_AT_YAY
    )


async def mark_announced(motion: ExpelMotion) -> None:
    motion.announced_at = datetime.now(timezone.utc)
    await motion.save(update_fields=["announced_at"])


def _subject(guild_tag: str, name: str | None) -> str:
    return f"**{name}** (`{guild_tag}`)" if name else f"`{guild_tag}`"


def render_open(motion: ExpelMotion, standing: Tally, name: str | None) -> str:
    """The live motion post: what's proposed, the bar, and turnout.

    Turnout without the split, deliberately, and no mover — see the
    module docstring. The bar is shown because a voter deciding whether
    to bother needs to know that not voting is a vote against.
    """
    closes = int(deadline(motion).timestamp())
    return "\n".join(
        [
            f"## Motion to expel {_subject(motion.guild_tag, name)}",
            "",
            f"If it carries, `{motion.guild_tag}`'s representatives are "
            "removed from the Hall and the guild is barred from rejoining.",
            "",
            "Every guild seated in the Hall has **one vote** — the last one "
            "cast by any of its representatives. It carries at "
            f"**{THRESHOLD_PERCENT}%** of the Hall voting yay, so **not voting "
            "counts against it**.",
            "",
            f"**{standing.voted}** of **{standing.electorate}** guilds have "
            f"voted · **{standing.needed}** yay needed to carry",
            f"Closes <t:{closes}:R>. Motions are private — nobody sees who "
            "moved this or who voted which way, and the split is published "
            "when it closes.",
        ]
    )


def render_call(motion: ExpelMotion, standing: Tally, name: str | None) -> str:
    """The one `@here`: enough guilds are behind this that it may carry.

    Short on purpose. Anyone it reaches has been interrupted, so it says
    what happened, what it would cost, and where to go — and nothing
    else, because the motion post itself is one click away.

    It says "at least ``ANNOUNCE_AT_YAY``" rather than the live count.
    The trigger is public and the two are the same number in every case
    but a shrinking electorate, so printing the count would leak the tally
    (§16.3) to buy nothing.
    """
    return (
        f"@here **at least {ANNOUNCE_AT_YAY} guilds have voted to expel "
        f"{_subject(motion.guild_tag, name)}.**\n"
        f"If {standing.needed} of the {standing.electorate} guilds seated in "
        "the Hall vote yay, they're removed from the server and barred. Not "
        "voting counts against them."
    )


def render_resolved(
    motion: ExpelMotion, standing: Tally, name: str | None
) -> str:
    """The post after the motion ends, with the split finally shown."""
    if motion.state == PASSED:
        headline = "carried"
        outcome = (
            f"`{motion.guild_tag}`'s representatives have been removed and the "
            "guild is barred from the Hall."
        )
    else:
        headline = "lapsed"
        outcome = (
            f"It closed after {settings.expel_motion_days} days without "
            f"reaching {THRESHOLD_PERCENT}%. `{motion.guild_tag}` stays in the "
            "Hall."
        )
    return "\n".join(
        [
            f"## Motion to expel {_subject(motion.guild_tag, name)} — {headline}",
            "",
            render_bar(standing),
            f"**{standing.yay}** yay · **{standing.nay}** nay · "
            f"**{standing.electorate - standing.voted}** did not vote "
            f"(**{standing.needed}** needed to carry)",
            "",
            outcome,
        ]
    )
