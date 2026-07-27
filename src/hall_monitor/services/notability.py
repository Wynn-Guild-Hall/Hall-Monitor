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

Any single signal being true marks the guild notable. Signals 4 and 5 are
the only two needing a per-guild request — both go through
``external.guild_stats``, which prefers Wynnpool and falls back to
Wynncraft — so they're evaluated last and skipped entirely when a cheaper
signal has already settled the answer.

The scheduler refreshes every ``NOTABILITY_REFRESH_SECONDS`` (default
3600 s); ``~script refresh_notability`` triggers the same sweep on demand.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from hall_monitor import external
from hall_monitor.db.models import Delegate, ForceOverride, NotabilityCache
from hall_monitor.external import wynncraft, wynnpool

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


@dataclass(frozen=True)
class _BulkContext:
    """Everything :func:`refresh_all` fetches once and shares across guilds."""

    tag_to_name: dict[str, str]  # canonical tag→name from any leaderboard
    avg_online: tuple[wynnpool.LeaderboardEntry, ...]
    guild_level: tuple[wynnpool.LeaderboardEntry, ...]
    season_boards: tuple[tuple[wynnpool.LeaderboardEntry, ...], ...]  # newest → oldest
    current_season_active: bool


async def is_notable(guild_tag: str) -> bool:
    """Fast cache read; falls through to an inline single-guild evaluation
    if the cache doesn't know this tag yet."""
    if await _has_active_notable_override(guild_tag):
        return True
    cached = await NotabilityCache.get_or_none(guild_tag=guild_tag)
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
    *, on_progress: ProgressCallback | None = None, exhaustive: bool = False
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

    ``exhaustive`` evaluates every signal even once one has answered,
    costing a per-guild request for every candidate. Pointless for
    deciding notability, necessary for deciding *thresholds*: the normal
    sweep can't tell you how many guilds clear 50 000 wars, because it
    stops asking as soon as something cheaper says yes.
    """
    if _refresh_lock.locked():
        logger.info("notability refresh already running; skipping this trigger")
        return None
    async with _refresh_lock:
        return await _refresh_all(on_progress, exhaustive)


async def _refresh_all(
    on_progress: "ProgressCallback | None" = None, exhaustive: bool = False
) -> RefreshSummary:
    started = time.monotonic()
    context = await _load_context()

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
            if await _evaluate_and_cache(tag, context, exhaustive=exhaustive):
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
    avg_online, guild_level, seasons = await asyncio.gather(
        wynnpool.average_online_leaderboard(),
        wynnpool.guild_level_leaderboard(),
        wynncraft.get_seasons(),
    )
    last_10 = seasons[-_SIGNAL_3_LAST_10:] if seasons else ()
    season_boards: tuple[tuple[wynnpool.LeaderboardEntry, ...], ...] = tuple(
        await asyncio.gather(
            *(wynnpool.season_rating(s.number) for s in reversed(last_10))
        )
    )
    tag_to_name: dict[str, str] = {}
    for board in (avg_online, guild_level, *season_boards):
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
        season_boards=season_boards,
        current_season_active=_any_active(seasons),
    )


def _any_active(seasons: tuple[wynncraft.Season, ...]) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    return any(s.start <= now <= s.end for s in seasons)


async def _evaluate_and_cache(
    tag: str, context: _BulkContext, *, exhaustive: bool = False
) -> bool:
    signals = await _evaluate(tag, context, exhaustive=exhaustive)
    result = any(signals.values())
    await NotabilityCache.update_or_create(
        guild_tag=tag,
        defaults={"is_notable": result, "signals_json": json.dumps(signals)},
    )
    return result


async def _evaluate_and_cache_single(tag: str) -> bool:
    return await _evaluate_and_cache(tag, await _load_context())


async def _evaluate(
    tag: str, context: _BulkContext, *, exhaustive: bool = False
) -> dict[str, bool | None]:
    """Compute each signal for ``tag`` and return a labelled dict.

    The dict is persisted verbatim to ``NotabilityCache.signals_json`` so a
    janitor can see exactly why a guild is (or isn't) notable without
    re-running the refresh. ``None`` there means *not evaluated* — an
    earlier signal had already settled it — as distinct from ``False``,
    which means checked and not met.

    Four of the six signals are answered by bulk payloads we already hold.
    Only territory ownership and war count need a per-guild fetch, so
    that request is deferred until the free signals have all come back
    negative. Notability is an ``any()``, so for a guild that
    already qualifies the fetch could not change the answer — and doing it
    anyway meant a full guild payload per candidate per hour, which is how
    a refresh sweep walks into a 429 and stays there.
    """
    signals: dict[str, bool | None] = {
        "top25_average_online": _signal_top25_avg_online(tag, context),
        "level_100_plus": _signal_level_100_plus(tag, context),
        # Leaderboard names come from the same source as the season boards,
        # so they match more reliably than the Wynncraft spelling would.
        "season_placement": _signal_season_placement(
            tag, context.tag_to_name.get(tag), context
        ),
        "territory_ownership": None,
        "war_count": None,
        "force_override": await _has_active_notable_override(tag),
    }
    if any(signals.values()) and not exhaustive:
        return signals

    guild = await _fetch_stats(tag, context)
    if guild is not None and context.tag_to_name.get(tag) is None:
        # A guild on no leaderboard has no name in the context; the
        # per-guild payload is the only place to learn it.
        signals["season_placement"] = _signal_season_placement(
            tag, guild.name, context
        )
    signals["territory_ownership"] = _signal_territory_ownership(
        guild, context.current_season_active
    )
    signals["war_count"] = _signal_war_count(guild)
    return signals


async def _fetch_stats(
    tag: str, context: _BulkContext
) -> external.GuildStats | None:
    """Per-guild numbers for ``tag``, from Wynnpool where it can answer."""
    return await external.guild_stats(context.tag_to_name.get(tag), tag)


def _signal_top25_avg_online(tag: str, ctx: _BulkContext) -> bool:
    return any(e.tag == tag and e.rank <= _SIGNAL_1_TOP_N for e in ctx.avg_online)


def _signal_level_100_plus(tag: str, ctx: _BulkContext) -> bool:
    for entry in ctx.guild_level:
        if entry.tag != tag:
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
            if e.tag == tag:
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


def _signal_territory_ownership(
    guild: "external.GuildStats | None", season_active: bool
) -> bool:
    if guild is None or not season_active:
        return False
    return guild.territories > _SIGNAL_4_MIN_TERRITORIES


def _signal_war_count(guild: "external.GuildStats | None") -> bool:
    if guild is None or guild.wars is None:
        return False
    return guild.wars > _SIGNAL_5_MIN_WARS


async def _has_active_notable_override(tag: str) -> bool:
    now = datetime.now(timezone.utc)
    override = await ForceOverride.filter(kind="notable", subject=tag).first()
    if override is None:
        return False
    return override.expires_at is None or override.expires_at > now
