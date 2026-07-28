"""Did a guild *hold* territory, or did it just have some when we looked?

Signal 4 is meant to identify guilds with real war capacity, and a
snapshot cannot do that. Any guild can take dozens of territories
*briefly*, and plenty of small ones can sit on a few indefinitely in
low contest ffa zones. For our purposes, what separates a major guild
is keeping **twenty for five days**.

So the sweep records one reading per tracked guild per pass, and this
module reads the series back.

Three decisions, each the difference between a measure and a number that
merely looks like one:

- **A fraction of the window, not a minimum.** A strong guild can lose
  most of its claim and take it back — down to three territories for two
  or three hours is an ordinary night, provided they reclaim it.
  Requiring every reading to clear the bar would disqualify exactly the
  guilds the signal exists to find. At hourly sampling
  :data:`SUSTAINED_FRACTION` leaves room to be under the bar for a full
  day in total across the five, which covers that comfortably while
  still failing a guild that dropped and never recovered.
- **Sweeps are the denominator, not the guild's own rows.** A guild that
  started holding two days ago has a flawless record over the two days
  we've watched it, and that is the claim we are specifically not
  making.
- **The window must be covered before it can be judged.** Until the
  series spans five days the signal reads false for everyone — so it
  goes quiet for the first five days after this ships. That's honest
  rather than unfortunate: nothing has yet demonstrated the thing being
  asked about.

Guilds that stop holding are recorded as **zero** rather than dropped. A
guild wiped off the map leaves no new rows if only holders are sampled,
so its last good readings would carry it for the rest of the window —
the exact failure the snapshot had.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from hall_monitor.db.models import TerritorySample
from hall_monitor.services import guild_tag as tags

logger = logging.getLogger(__name__)

# How long a guild has to keep it up. Five days is the brief's number.
WINDOW = timedelta(days=5)

# What share of the window's readings must clear the threshold.
SUSTAINED_FRACTION = 0.8

# Kept past the window so a late sweep still has a full one to read, and
# so a report can show what fell off the edge.
RETENTION = WINDOW + timedelta(days=1)


@dataclass(frozen=True)
class Holding:
    """One guild's record across the window."""

    readings: int
    above: int
    low: int
    high: int


@dataclass(frozen=True)
class Window:
    """The series as a whole, and whether it's long enough to judge on."""

    sweeps: int
    watched: timedelta
    threshold: int
    holdings: dict[str, Holding]

    @property
    def covered(self) -> bool:
        """Whether we've been watching for as long as we're asking about.

        Measured from the *oldest reading we still hold*, not from the
        span of readings inside the window — those can only span the full
        window if a sample lands exactly on its edge, so comparing them
        to it would mean `covered` was essentially never true. Retention
        runs a day longer than the window precisely so this can be asked.
        """
        return self.sweeps > 1 and self.watched >= WINDOW

    def fraction(self, guild_tag: str) -> float:
        holding = self.holdings.get(tags.normalise(guild_tag))
        if holding is None or not self.sweeps:
            return 0.0
        return holding.above / self.sweeps

    def sustained(self, guild_tag: str) -> bool:
        """Whether this guild held above the threshold *for* the window."""
        return self.covered and self.fraction(guild_tag) >= SUSTAINED_FRACTION

    def line(self, guild_tag: str) -> str:
        """A readable account of one guild's record, for reports."""
        if not self.covered:
            days = self.watched.total_seconds() / 86400
            return (
                f"not enough history yet — {self.sweeps} reading(s) over "
                f"{days:.1f}d of a {WINDOW.days}d window"
            )
        holding = self.holdings.get(tags.normalise(guild_tag))
        if holding is None:
            return f"held nothing across {self.sweeps} readings"
        return (
            f"above {self.threshold} in {holding.above}/{self.sweeps} readings "
            f"({self.fraction(guild_tag):.0%}), ranging {holding.low}–{holding.high}"
        )


async def record(holdings: dict[str, int], *, at: datetime | None = None) -> int:
    """Write one reading per tracked guild, and prune what's aged out.

    ``holdings`` is the live map, tag → count. Guilds already being
    tracked but absent from it are recorded as zero.

    Every row in a pass shares one timestamp, which is what lets a read
    count sweeps by counting distinct timestamps.
    """
    at = at or datetime.now(timezone.utc)
    live = {tags.normalise(tag): count for tag, count in holdings.items()}
    spellings = {tags.normalise(tag): tag for tag in holdings}

    tracked = set()
    for stored in await TerritorySample.filter(
        sampled_at__gte=at - WINDOW
    ).values_list("guild_tag", flat=True):
        folded = tags.normalise(stored)
        tracked.add(folded)
        # Keep the spelling the guild was first recorded under, so a
        # zero-fill row doesn't quietly introduce a folded duplicate of a
        # tag we already had.
        spellings.setdefault(folded, stored)
    # Zeroes first so the live counts win the merge. The other order
    # silently records every holder as holding nothing.
    readings = {folded: 0 for folded in tracked} | live

    await TerritorySample.bulk_create(
        [
            TerritorySample(
                guild_tag=spellings.get(folded, folded),
                territories=count,
                sampled_at=at,
            )
            for folded, count in readings.items()
        ]
    )
    await TerritorySample.filter(sampled_at__lt=at - RETENTION).delete()
    return len(readings)


async def load(threshold: int, *, now: datetime | None = None) -> Window:
    """Summarise the window in one query.

    A sweep evaluates a couple of hundred guilds, so this is read once
    and shared rather than asked per guild.
    """
    now = now or datetime.now(timezone.utc)
    rows = await TerritorySample.filter(sampled_at__gte=now - WINDOW).values(
        "guild_tag", "territories", "sampled_at"
    )
    earliest = await TerritorySample.all().order_by("sampled_at").first()
    watched = now - earliest.sampled_at if earliest else timedelta()
    if not rows:
        return Window(
            sweeps=0, watched=watched, threshold=threshold, holdings={}
        )

    stamps = {row["sampled_at"] for row in rows}
    counts: dict[str, list[int]] = {}
    for row in rows:
        counts.setdefault(tags.normalise(row["guild_tag"]), []).append(
            row["territories"]
        )

    return Window(
        sweeps=len(stamps),
        watched=watched,
        threshold=threshold,
        holdings={
            tag: Holding(
                readings=len(series),
                above=sum(1 for count in series if count > threshold),
                low=min(series),
                high=max(series),
            )
            for tag, series in counts.items()
        },
    )
