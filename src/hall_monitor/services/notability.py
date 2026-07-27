"""Aggregate the 6 guild-notability signals into a single boolean.

Signals
-------
1. **Top-25 average online** on Wynnpool's ``guild-average-online``
   leaderboard (last 5 days).
2. **Level 100+** on Wynnpool's ``guildLevel`` leaderboard.
3. **Season placement** across recent seasons — any of:
   top-3 in any of the last 10 seasons; top-10 in any of the last 5;
   mean rank across the last 5 seasons ≤ 25.
4. **Territory ownership** > 20 while a Wynncraft season is currently
   running. (Approximated with the current snapshot; the full 5-day
   average would require historical polling we don't yet do.)
5. **War count** > 50 000.
6. **Force override** — a `ForceOverride(kind="notable", subject=tag)`
   row with no expiry or an expiry in the future.

Any single signal being true marks the guild notable. Every one is
answered from bulk Wynnpool leaderboards, so a sweep costs a fixed ~20
requests regardless of how many guilds it evaluates. Signals 4 and 5 rely
on a property of top-N boards: while the board's floor sits below our
threshold, a guild absent from it must be under that threshold. When that
stops holding, ``external.guild_stats`` is the per-guild fallback.

The scheduler refreshes every ``NOTABILITY_REFRESH_SECONDS`` (default
3600 s); ``~script refresh_notability`` triggers the same sweep on demand.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from hall_monitor import external
from hall_monitor.db.models import Delegate, ForceOverride, NotabilityCache
from hall_monitor.external import wynncraft, wynnpool
from hall_monitor.services import guild_tag as tags

logger = logging.getLogger(__name__)

_SIGNAL_1_TOP_N = 25
_SIGNAL_2_MIN_LEVEL = 100
_SIGNAL_3_TOP_3 = 3
_SIGNAL_3_TOP_10 = 10
_SIGNAL_3_MEAN_TOP = 25
_SIGNAL_3_LAST_10 = 10
_SIGNAL_3_LAST_5 = 5
_SIGNAL_4_MIN_TERRITORIES = 20
_SIGNAL_5_MIN_WARS = 50_000


# Boards that answer a signal directly.
_SIGNAL_BOARDS = ("guild-average-online", "guildLevel", "guildWars", "guildTerritories")

# Boards that answer nothing on their own but widen the candidate set —
# a guild ranked for raids may still qualify on level, wars or seasons,
# and it can't do so if we never look at it.
_CANDIDATE_BOARDS = (
    "guildTotalRaids",
    "grootslangSrGuilds",
    "orphionSrGuilds",
    "colossusSrGuilds",
    "frumaSrGuilds",
    "namelessSrGuilds",
)


@dataclass(frozen=True)
class _BulkContext:
    """Everything :func:`refresh_all` fetches once and shares across guilds."""

    tag_to_name: dict[str, str]  # canonical tag→name from any leaderboard
    avg_online: tuple[wynnpool.LeaderboardEntry, ...]
    guild_level: tuple[wynnpool.LeaderboardEntry, ...]
    wars: tuple[wynnpool.LeaderboardEntry, ...]
    territories: tuple[wynnpool.LeaderboardEntry, ...]
    season_boards: tuple[tuple[wynnpool.LeaderboardEntry, ...], ...]  # newest → oldest
    current_season_active: bool
    # A top-N board proves a "> threshold" signal false by omission only
    # while its floor sits below that threshold. If Wynncraft inflation
    # ever pushes the 100th guild past ours, omission stops meaning
    # anything and we have to ask per guild again.
    wars_board_decisive: bool = True
    territories_board_decisive: bool = True
    # tag_to_name keeps Wynncraft's spelling, because that's what gets
    # cached and displayed; this is the same map keyed for lookup.
    folded_to_name: dict[str, str] = field(default_factory=dict)

    def name_for(self, tag: str) -> str | None:
        return self.folded_to_name.get(tags.normalise(tag))


async def is_notable(guild_tag: str) -> bool:
    """Fast cache read; falls through to an inline single-guild evaluation
    if the cache doesn't know this tag yet."""
    if await _has_active_notable_override(guild_tag):
        return True
    cached = await NotabilityCache.get_or_none(guild_tag__iexact=guild_tag)
    if cached is not None:
        return cached.is_notable
    return await _evaluate_and_cache_single(guild_tag)


@dataclass(frozen=True)
class RefreshSummary:
    """Outcome of one sweep, for the operator who triggered it."""

    evaluated: int
    failed: int
    notable: int
    seconds: float


_refresh_lock = asyncio.Lock()


def is_refreshing() -> bool:
    """Whether a sweep is running right now."""
    return _refresh_lock.locked()


ProgressCallback = Callable[[int, int], Awaitable[None]]


async def refresh_all(
    *, on_progress: ProgressCallback | None = None
) -> RefreshSummary | None:
    """Recompute notability for every candidate guild and update the cache.

    Candidates = the union of guilds visible on any Wynnpool leaderboard,
    guilds we already have Delegate rows for, and guilds with a force
    override — that way manually-added guilds still get a cache row.

    Single-flight: a sweep takes minutes and makes a per-guild request for
    every guild the cheap signals don't settle, so a second one overlapping
    the first would double that load against APIs already close to their
    limit. A trigger arriving mid-sweep returns ``None`` rather than
    queueing behind it — by the time it ran, its results would be the
    ones the running sweep is already producing.

    ``on_progress(done, total)`` is awaited after each guild. It fires per
    guild rather than on a timer so the caller can decide its own cadence;
    a Discord message edit, for instance, has to be throttled well below
    one-per-guild.

    """
    if _refresh_lock.locked():
        logger.info("notability refresh already running; skipping this trigger")
        return None
    async with _refresh_lock:
        return await _refresh_all(on_progress)


async def _refresh_all(on_progress: "ProgressCallback | None" = None) -> RefreshSummary:
    started = time.monotonic()
    context = await _load_context()
    # Keep the miss path fast: it reuses whatever the last sweep loaded.
    _remember_context(context)

    delegate_tags = {
        row["guild_tag"]
        for row in await Delegate.all().values("guild_tag")
    }
    override_tags = {
        row["subject"]
        for row in await ForceOverride.filter(kind="notable").values("subject")
    }
    tags = set(context.tag_to_name) | delegate_tags | override_tags

    ordered = sorted(tags)
    notable = 0
    failed = 0
    for index, tag in enumerate(ordered, start=1):
        try:
            if await _evaluate_and_cache(tag, context):
                notable += 1
        except Exception:
            # One guild's API hiccup or write contention shouldn't cost us
            # the other ninety-nine; its cache row keeps its prior value.
            failed += 1
            logger.exception("notability refresh failed for %s", tag)
        if on_progress is not None:
            try:
                await on_progress(index, len(ordered))
            except Exception:
                # Reporting is a nicety; a broken callback must not cost
                # us the sweep it's reporting on.
                logger.exception("notability refresh progress callback failed")
    summary = RefreshSummary(
        evaluated=len(tags) - failed,
        failed=failed,
        notable=notable,
        seconds=time.monotonic() - started,
    )
    logger.info(
        "notability refresh: %d evaluated, %d notable, %d failed in %.1fs",
        summary.evaluated,
        summary.notable,
        summary.failed,
        summary.seconds,
    )
    return summary


# --------------------------------------------------------------------------
# Internal — evaluation
# --------------------------------------------------------------------------


async def _load_context() -> _BulkContext:
    avg_online, guild_level, wars, territories, seasons = await asyncio.gather(
        wynnpool.average_online_leaderboard(),
        wynnpool.guild_level_leaderboard(),
        wynnpool.wars_leaderboard(),
        wynnpool.territories_leaderboard(),
        wynncraft.get_seasons(),
    )
    candidate_boards = await asyncio.gather(
        *(wynnpool.leaderboard(name) for name in _CANDIDATE_BOARDS),
        return_exceptions=True,
    )
    extra_boards = []
    for name, board in zip(_CANDIDATE_BOARDS, candidate_boards):
        if isinstance(board, BaseException):
            # Candidate-only, so losing one costs coverage, not correctness.
            logger.warning("candidate board %s unavailable: %s", name, board)
            continue
        extra_boards.append(board)
    last_10 = seasons[-_SIGNAL_3_LAST_10:] if seasons else ()
    season_boards: tuple[tuple[wynnpool.LeaderboardEntry, ...], ...] = tuple(
        await asyncio.gather(
            *(wynnpool.season_rating(s.number) for s in reversed(last_10))
        )
    )
    tag_to_name: dict[str, str] = {}
    for board in (avg_online, guild_level, wars, territories, *extra_boards, *season_boards):
        for entry in board:
            # Season boards carry no prefix, so their entries arrive with
            # tag=None. Letting one into the map poisons every later
            # `sorted()` over the candidate tags.
            if entry.tag is None:
                continue
            tag_to_name.setdefault(entry.tag, entry.name)
    return _BulkContext(
        tag_to_name=tag_to_name,
        avg_online=avg_online,
        guild_level=guild_level,
        wars=wars,
        territories=territories,
        season_boards=season_boards,
        current_season_active=_any_active(seasons),
        folded_to_name={tags.normalise(k): v for k, v in tag_to_name.items()},
        wars_board_decisive=_board_decides(wars, _SIGNAL_5_MIN_WARS, "guildWars"),
        territories_board_decisive=_board_decides(
            territories, _SIGNAL_4_MIN_TERRITORIES, "guildTerritories"
        ),
    )


def _board_decides(
    board: tuple[wynnpool.LeaderboardEntry, ...], threshold: float, name: str
) -> bool:
    """Whether omission from ``board`` proves a guild is under ``threshold``.

    True while the lowest-ranked entry is at or below the threshold: a
    guild above it would have displaced that entry.
    """
    values = [e.value for e in board if e.value is not None]
    if not values:
        # Nothing to reason from. Report "decisive" anyway: the signal will
        # read false for everyone, which is what a missing board leaves us
        # with regardless, and the alternative — falling back per guild —
        # spends a request each on hundreds of guilds to learn the same.
        logger.warning(
            "%s came back empty; its signal reads false for every guild", name
        )
        return True
    floor = min(values)
    if floor > threshold:
        logger.warning(
            "%s only reaches down to %s, above our threshold of %s — "
            "guilds off the board can no longer be ruled out from it",
            name,
            floor,
            threshold,
        )
        return False
    return True


def _board_value(
    board: tuple[wynnpool.LeaderboardEntry, ...], tag: str
) -> float | None:
    for entry in board:
        if tags.matches(entry.tag, tag):
            return entry.value
    return None


def _any_active(seasons: tuple[wynncraft.Season, ...]) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    return any(s.start <= now <= s.end for s in seasons)


async def _evaluate_and_cache(tag: str, context: _BulkContext) -> bool:
    signals = await _evaluate(tag, context)
    result = any(signals.values())
    await NotabilityCache.update_or_create(
        guild_tag=tag,
        defaults={"is_notable": result, "signals_json": json.dumps(signals)},
    )
    return result


async def _evaluate_and_cache_single(tag: str) -> bool:
    return await _evaluate_and_cache(tag, await _memoised_context())


_CONTEXT_TTL_S = 3600
_context_memo: "tuple[float, _BulkContext] | None" = None


def _remember_context(context: _BulkContext) -> None:
    global _context_memo
    _context_memo = (time.monotonic(), context)


def reset_context_memo() -> None:
    """Drop the memoised bulk context. Tests use this to keep module state
    from leaking between cases."""
    global _context_memo
    _context_memo = None


async def _memoised_context() -> _BulkContext:
    """The bulk leaderboards, reused from the last sweep when they're fresh.

    ``is_notable`` falls through to a single-guild evaluation on a cache
    miss, and rebuilding the context there means twenty-odd sequential
    leaderboard fetches — on a request a player is waiting on in
    Minecraft. Picolimbo blocks that client's connection handling for the
    duration, keep-alives included, so a slow answer here surfaces to them
    as a dropped connection rather than a slow one.

    The hourly sweep keeps this warm, so the miss path normally costs
    nothing at all. The TTL only matters if no sweep has run.
    """
    if _context_memo is not None:
        loaded_at, context = _context_memo
        if time.monotonic() - loaded_at < _CONTEXT_TTL_S:
            return context
    context = await _load_context()
    _remember_context(context)
    return context


async def _evaluate(tag: str, context: _BulkContext) -> dict[str, bool | None]:
    """Compute each signal for ``tag`` and return a labelled dict.

    The dict is persisted verbatim to ``NotabilityCache.signals_json`` so a
    janitor can see exactly why a guild is (or isn't) notable without
    re-running the refresh.

    Every signal is answered from bulk leaderboards the sweep already
    holds, so evaluating a guild costs no request of its own. Territory
    ownership and war count lean on a property of top-N boards: while the
    board's floor sits below our threshold, a guild missing from it must
    be under that threshold, because otherwise it would have displaced the
    bottom entry. ``_board_decides`` checks that each sweep, and only when
    it stops holding do we fall back to asking per guild.
    """
    wars = _board_value(context.wars, tag)
    territories = _board_value(context.territories, tag)

    if wars is None and not context.wars_board_decisive:
        wars = await _stat(tag, context, "wars")
    if territories is None and not context.territories_board_decisive:
        territories = await _stat(tag, context, "territories")

    return {
        "top25_average_online": _signal_top25_avg_online(tag, context),
        "level_100_plus": _signal_level_100_plus(tag, context),
        # Leaderboard names come from the same source as the season boards,
        # so they match more reliably than the Wynncraft spelling would.
        "season_placement": _signal_season_placement(
            tag, context.name_for(tag), context
        ),
        "territory_ownership": _signal_territory_ownership(
            territories, context.current_season_active
        ),
        "war_count": _signal_war_count(wars),
        "force_override": await _has_active_notable_override(tag),
    }


async def _stat(tag: str, context: _BulkContext, attribute: str) -> float | None:
    """One per-guild number, for when a board has stopped being decisive."""
    stats = await _fetch_stats(tag, context)
    return None if stats is None else getattr(stats, attribute)


async def _fetch_stats(
    tag: str, context: _BulkContext
) -> external.GuildStats | None:
    """Per-guild numbers for ``tag``, from Wynnpool where it can answer."""
    return await external.guild_stats(context.name_for(tag), tag)


def _signal_top25_avg_online(tag: str, ctx: _BulkContext) -> bool:
    return any(
        tags.matches(e.tag, tag) and e.rank <= _SIGNAL_1_TOP_N for e in ctx.avg_online
    )


def _signal_level_100_plus(tag: str, ctx: _BulkContext) -> bool:
    for entry in ctx.guild_level:
        if not tags.matches(entry.tag, tag):
            continue
        # If the payload reports a value, gate on it; otherwise treat
        # appearance on the leaderboard as sufficient.
        if entry.value is None:
            return True
        return entry.value >= _SIGNAL_2_MIN_LEVEL
    return False


def _signal_season_placement(
    tag: str, name: str | None, ctx: _BulkContext
) -> bool:
    """Whether ``tag`` placed well enough in recent seasons.

    Season boards identify guilds by name only — Wynnpool's season-rating
    payload has no prefix field — so a tag-only match silently never fires.
    Names are compared case-insensitively; the tag is still checked first
    in case the shape gains a prefix later.
    """
    boards = ctx.season_boards  # newest first
    if not boards:
        return False

    folded = name.casefold() if name else None

    def rank_in(board: tuple[wynnpool.LeaderboardEntry, ...]) -> int | None:
        for e in board:
            if tags.matches(e.tag, tag):
                return e.rank
            if folded and e.name and e.name.casefold() == folded:
                return e.rank
        return None

    last_10 = boards[: _SIGNAL_3_LAST_10]
    last_5 = boards[: _SIGNAL_3_LAST_5]

    # Sub-condition A: top-3 in any of the last 10 seasons.
    for board in last_10:
        rank = rank_in(board)
        if rank is not None and rank <= _SIGNAL_3_TOP_3:
            return True

    # Sub-condition B: top-10 in any of the last 5 seasons.
    for board in last_5:
        rank = rank_in(board)
        if rank is not None and rank <= _SIGNAL_3_TOP_10:
            return True

    # Sub-condition C: mean rank across last 5 seasons ≤ 25.
    ranks = [r for r in (rank_in(b) for b in last_5) if r is not None]
    if ranks and len(ranks) == len(last_5):
        if sum(ranks) / len(ranks) <= _SIGNAL_3_MEAN_TOP:
            return True

    return False


def _signal_territory_ownership(territories: float | None, season_active: bool) -> bool:
    if territories is None or not season_active:
        return False
    return territories > _SIGNAL_4_MIN_TERRITORIES


def _signal_war_count(wars: float | None) -> bool:
    if wars is None:
        return False
    return wars > _SIGNAL_5_MIN_WARS


async def _has_active_notable_override(tag: str) -> bool:
    now = datetime.now(timezone.utc)
    override = await ForceOverride.filter(
        kind="notable", subject__iexact=tag
    ).first()
    if override is None:
        return False
    return override.expires_at is None or override.expires_at > now
