"""Aggregate the 6 guild-notability signals into a single boolean.

Signals
-------
1. **Top-25 average online** on Wynnpool's ``guild-average-online``
   leaderboard (last 5 days).
2. **Level 100+** on Wynnpool's ``guildLevel`` leaderboard.
3. **Season placement** across recent seasons — any of:
   top-3 in any of the last 10 seasons; top-10 in any of the last 5;
   mean rank across the last 5 seasons ≤ 25.
4. **Territory ownership** — above 20 territories *sustained across five
   days* while a Wynncraft season is running. Counted from Wynncraft's
   live territory map, sampled each sweep and read back from
   ``services/territory_history.py``. A snapshot won't do: any guild can
   take 150 territories for five minutes, and holding twenty for five
   days is what actually needs on-call war teams and eco capacity.
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
from hall_monitor.services import guild_tag as tags, territory_history

logger = logging.getLogger(__name__)

_SIGNAL_1_TOP_N = 25
_SIGNAL_2_MIN_LEVEL = 100
_SIGNAL_3_TOP_3 = 3
_SIGNAL_3_TOP_10 = 10
_SIGNAL_3_MEAN_TOP = 25
_SIGNAL_3_LAST_10 = 10
_SIGNAL_3_LAST_5 = 5
MIN_TERRITORIES = 20
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
    # Live holdings from Wynncraft's territory map, by comparison key.
    # A dict rather than a board because the map is *complete*: a guild
    # that isn't in it holds nothing, which is a fact and not a gap.
    territories: dict[str, int]
    # The last five days of those readings. The signal is about holding,
    # not about having held when we happened to look.
    territory_window: territory_history.Window
    season_boards: tuple[tuple[wynnpool.LeaderboardEntry, ...], ...]  # newest → oldest
    current_season_active: bool
    # A top-N board proves a "> threshold" signal false by omission only
    # while its floor sits below that threshold. If Wynncraft inflation
    # ever pushes the 100th guild past ours, omission stops meaning
    # anything and we have to ask per guild again.
    wars_board_decisive: bool = True
    # tag_to_name keeps Wynncraft's spelling, because that's what gets
    # cached and displayed; this is the same map keyed for lookup.
    folded_to_name: dict[str, str] = field(default_factory=dict)

    def name_for(self, tag: str) -> str | None:
        return self.folded_to_name.get(tags.normalise(tag))

    def territories_for(self, tag: str) -> int:
        """Territories held right now. Absent from the map means none."""
        return self.territories.get(tags.normalise(tag), 0)

    def learn(self, tag: str, name: str) -> None:
        """Record a name the leaderboards didn't carry.

        The dataclass is frozen so the *shape* can't change mid-sweep;
        the maps inside it are a cache being filled in, which is a
        different thing. See :func:`_learn_missing_names`.
        """
        self.tag_to_name.setdefault(tag, name)
        self.folded_to_name.setdefault(tags.normalise(tag), name)


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
    await _learn_missing_names(context, ordered)
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
    avg_online, guild_level, wars, territory_map, seasons = await asyncio.gather(
        wynnpool.average_online_leaderboard(),
        wynnpool.guild_level_leaderboard(),
        wynnpool.wars_leaderboard(),
        # Wynncraft's own territory map rather than Wynnpool's
        # `guildTerritories` board. That board is a snapshot that can lag
        # badly — it credited two guilds with 61 and 57 territories while
        # the game said they held none — and a stale count here doesn't
        # make a guild slightly wrong, it makes it notable.
        wynncraft.territory_holdings(),
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
    season_boards = await _season_boards(last_10)
    tag_to_name: dict[str, str] = {}
    # Territory holders are candidates in their own right: a guild can
    # hold half the map without appearing on any Wynnpool board, and
    # dropping it from the candidate set would mean never evaluating the
    # one signal it obviously qualifies on.
    for holder in territory_map:
        tag_to_name.setdefault(holder.prefix, holder.name)
    for board in (avg_online, guild_level, wars, *extra_boards, *season_boards):
        for entry in board:
            # Season boards carry no prefix, so their entries arrive with
            # tag=None. Letting one into the map poisons every later
            # `sorted()` over the candidate tags.
            if entry.tag is None:
                continue
            tag_to_name.setdefault(entry.tag, entry.name)
    # Record before reading, so this sweep's reading is part of the window
    # it's about to judge on.
    await territory_history.record(
        {holder.prefix: holder.territories for holder in territory_map}
    )
    window = await territory_history.load(MIN_TERRITORIES)
    if not window.covered:
        logger.info(
            "territory history: %d reading(s) over %.1fd of a %dd window — the "
            "territory signal stays false until it's covered",
            window.sweeps,
            window.watched.total_seconds() / 86400,
            territory_history.WINDOW.days,
        )

    return _BulkContext(
        tag_to_name=tag_to_name,
        avg_online=avg_online,
        guild_level=guild_level,
        wars=wars,
        territories={
            tags.normalise(holder.prefix): holder.territories
            for holder in territory_map
        },
        territory_window=window,
        season_boards=season_boards,
        current_season_active=_any_active(seasons),
        folded_to_name={tags.normalise(k): v for k, v in tag_to_name.items()},
        wars_board_decisive=_board_decides(wars, _SIGNAL_5_MIN_WARS, "guildWars"),
    )


# A finished season's ratings never change again, so its board is
# fetched once per process and kept. That's most of the sweep's request
# budget: ten season boards an hour, of which at most one is live.
_season_board_cache: dict[int, tuple[wynnpool.LeaderboardEntry, ...]] = {}


def reset_season_cache() -> None:
    """Drop the cached season boards. For tests."""
    _season_board_cache.clear()


async def _season_boards(
    last_10: tuple[wynncraft.Season, ...],
) -> tuple[tuple[wynnpool.LeaderboardEntry, ...], ...]:
    """The recent season boards, newest first, fetched as little as possible.

    Two economies, both of which matter because Wynnpool rate-limits per
    IP and this is the biggest single burst we make:

    - **A finished season is immutable.** Its board is fetched once and
      kept for the life of the process; only a season still running is
      re-read. That takes the steady-state cost from ten requests an hour
      to one.
    - **A board that fails is an empty board, not a failed sweep.** One
      429 here used to abort the whole refresh — the `gather` had no
      `return_exceptions` — which is how `~script refresh_notability`
      came back with "that broke on my end". An empty board keeps the
      list positionally aligned, so "the last five seasons" still means
      the last five; the guild simply doesn't place in the one we
      couldn't read, which can only make signal 3 read false. A missing
      season is worth less than a missing sweep.
    """
    now = datetime.now(timezone.utc).isoformat()
    ordered = list(reversed(last_10))  # newest first
    needed = [
        season
        for season in ordered
        if season.number not in _season_board_cache or season.end > now
    ]
    fetched = await asyncio.gather(
        *(wynnpool.season_rating(season.number) for season in needed),
        return_exceptions=True,
    )
    for season, board in zip(needed, fetched):
        if isinstance(board, BaseException):
            logger.warning(
                "season board %s unavailable: %s; treating it as empty this sweep",
                season.number,
                board,
            )
            _season_board_cache.setdefault(season.number, ())
            continue
        _season_board_cache[season.number] = board
    return tuple(_season_board_cache.get(season.number, ()) for season in ordered)


async def _learn_missing_names(context: _BulkContext, candidates: list[str]) -> None:
    """Fill in the names the leaderboards didn't carry.

    Names normally arrive free with the boards, but a guild can be a
    candidate without appearing on one: delegate guilds and forced tags
    join the set on their own, and season boards publish no prefix at
    all. ``VETS`` is that case — it had no name anywhere, so the roster
    printed it as ``**VETS** (`VETS`)``.

    Cheap by construction. A name already in the cache is reused rather
    than re-fetched, so a guild costs one Wynncraft prefix lookup *ever*,
    and the candidates that need one are only the handful not on any of
    the ten boards. The exception is a tag nothing knows — an invented
    one somebody forced — which is asked again each sweep; one request an
    hour, and it starts working the day the guild becomes real.

    Worth knowing this also feeds signal 3: season boards identify guilds
    by name, so a guild whose name we never learned could not match one.
    """
    unknown = [tag for tag in candidates if context.name_for(tag) is None]
    if not unknown:
        return

    remembered = {
        tags.normalise(row["guild_tag"]): row["guild_name"]
        for row in await NotabilityCache.filter(guild_name__isnull=False).values(
            "guild_tag", "guild_name"
        )
    }
    asked = learned = 0
    for tag in unknown:
        if name := remembered.get(tags.normalise(tag)):
            context.learn(tag, name)
            continue
        asked += 1
        if name := await external.guild_name_for(tag):
            context.learn(tag, name)
            learned += 1
    if asked:
        logger.info(
            "notability: %d candidate(s) off every board; asked Wynncraft, "
            "learned %d name(s)",
            asked,
            learned,
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
    defaults: dict[str, object] = {
        "is_notable": result,
        "signals_json": json.dumps(signals),
        "metrics_json": json.dumps(_metrics(tag, context)),
        # Where the roster sorts this guild (services/roster.py). Written
        # every sweep including when it's `None`: falling off the level
        # board is a real change, and a remembered rank would keep the
        # guild sorted as though it hadn't.
        "level_rank": _level_rank(tag, context),
    }
    # The name only appears on boards, so a guild the sweep can't see on
    # one — a forced-notable tag, say — keeps whatever name we last knew
    # rather than being blanked back to nothing.
    if (name := context.name_for(tag)) is not None:
        defaults["guild_name"] = name
    await NotabilityCache.update_or_create(guild_tag=tag, defaults=defaults)
    return result


def _level_rank(tag: str, context: _BulkContext) -> int | None:
    return _board_rank(context.guild_level, tag)


def _board_rank(
    board: tuple[wynnpool.LeaderboardEntry, ...], tag: str
) -> int | None:
    for entry in board:
        if tags.matches(entry.tag, tag):
            return entry.rank
    return None


def _metrics(tag: str, context: _BulkContext) -> dict[str, float | None]:
    """The numbers behind the signals, for ranking guilds against each other.

    The signals answer "does this guild qualify"; these answer "by how
    much". Nothing in notability itself needs them — a guild is notable
    on any single signal, and no amount of extra margin makes it more so
    — but they're the only way to order guilds when there are more
    qualifying than there are emote slots (:func:`strength`).
    """
    ranks = season_ranks(tag, context.name_for(tag), context)
    placed = [rank for rank in ranks if rank is not None]
    return {
        "average_online_rank": _board_rank(context.avg_online, tag),
        "guild_level": _board_value(context.guild_level, tag),
        "best_season_rank": min(placed) if placed else None,
        # Kept so a report can say *which* placement rule carried a guild,
        # and show the record it was read from, without refetching ten
        # season boards to find out.
        "season_ranks": ranks,
        "season_rules": season_rules(ranks),
        "territories": context.territories_for(tag),
        # The record behind the signal, so a report can say *why* rather
        # than only what.
        "territory_sustained_fraction": round(
            context.territory_window.fraction(tag), 3
        ),
        "territory_window_covered": context.territory_window.covered,
        "wars": _board_value(context.wars, tag),
    }


# The signals in DESIGN.md §4 order. Ties on the *number* of matched
# signals are broken by their values in this order, so a guild matching
# signal 1 more strongly outranks one matching signal 2 more strongly.
SIGNAL_ORDER = (
    "top25_average_online",
    "level_100_plus",
    "season_placement",
    "territory_ownership",
    "war_count",
    "force_override",
)


def strength(
    signals: dict[str, object], metrics: dict[str, object]
) -> tuple[float, ...]:
    """How strongly a guild qualifies, as a sortable tuple. Bigger is stronger.

    First element is **how many signals it matched**, which is the
    headline: a guild notable on four counts is more securely part of the
    Hall than one scraping in on a single leaderboard. The rest break
    ties with the underlying numbers, signal by signal in
    :data:`SIGNAL_ORDER`.

    An unmatched signal contributes zero, so this never rewards a guild
    for being *nearly* good at something it didn't qualify on. Rank-based
    signals are inverted — rank 1 is the strongest, not the weakest —
    which is the one place a raw value would sort exactly backwards.
    """
    matched = sum(1 for name in SIGNAL_ORDER if signals.get(name))
    return (float(matched), *(_margin(name, signals, metrics) for name in SIGNAL_ORDER))


def _margin(
    name: str, signals: dict[str, object], metrics: dict[str, object]
) -> float:
    if not signals.get(name):
        return 0.0
    if name == "top25_average_online":
        rank = metrics.get("average_online_rank")
        return 0.0 if rank is None else float(_SIGNAL_1_TOP_N + 1 - rank)
    if name == "level_100_plus":
        return float(metrics.get("guild_level") or 0)
    if name == "season_placement":
        rank = metrics.get("best_season_rank")
        # Boards are top-100, so 101 - rank keeps first place highest and
        # never goes negative.
        return 0.0 if rank is None else float(max(0, 101 - rank))
    if name == "territory_ownership":
        return float(metrics.get("territories") or 0)
    if name == "war_count":
        return float(metrics.get("wars") or 0)
    # A force override has no magnitude — a janitor said yes, and that's
    # the whole of it.
    return 1.0


async def strength_by_tag() -> dict[str, tuple[float, ...]]:
    """Comparison key → strength, for every guild in the cache.

    One query. Guilds with no cached row — a fresh force override, say —
    are simply absent, and callers sort them last, which is right: we
    know nothing that would justify putting them ahead of a guild we've
    actually measured.
    """
    by_tag = {}
    for row in await NotabilityCache.all().values(
        "guild_tag", "signals_json", "metrics_json"
    ):
        try:
            signals = json.loads(row["signals_json"] or "{}")
            metrics = json.loads(row["metrics_json"] or "{}")
        except ValueError:
            continue
        by_tag[tags.normalise(row["guild_tag"])] = strength(signals, metrics)
    return by_tag


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

    Every signal is answered from bulk data the sweep already holds, so
    evaluating a guild costs no request of its own. Territories come from
    Wynncraft's complete territory map, so a guild missing from it holds
    none — a fact, not a gap. War count leans on a property of top-N
    boards instead: while the board's floor sits below our threshold, a
    guild missing from it must be under that threshold, because otherwise
    it would have displaced the bottom entry. ``_board_decides`` checks
    that each sweep, and only when it stops holding do we ask per guild.
    """
    wars = _board_value(context.wars, tag)
    if wars is None and not context.wars_board_decisive:
        wars = await _stat(tag, context, "wars")

    return {
        "top25_average_online": _signal_top25_avg_online(tag, context),
        "level_100_plus": _signal_level_100_plus(tag, context),
        # Leaderboard names come from the same source as the season boards,
        # so they match more reliably than the Wynncraft spelling would.
        "season_placement": _signal_season_placement(
            tag, context.name_for(tag), context
        ),
        "territory_ownership": _signal_territory_ownership(
            context.territory_window.sustained(tag),
            context.current_season_active,
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


# The three ways a guild can place well enough, kept apart so a report
# can say *which* one carried it. They're very different claims — one
# outstanding season years ago versus a steady record — and collapsing
# them into a single boolean hides the distinction exactly where someone
# is deciding whether a threshold is right.
SEASON_TOP3_LAST10 = "top3_last10"
SEASON_TOP10_LAST5 = "top10_last5"
SEASON_MEAN_LAST5 = "mean5_within_25"

SEASON_RULES = (SEASON_TOP3_LAST10, SEASON_TOP10_LAST5, SEASON_MEAN_LAST5)

SEASON_RULE_LABELS = {
    SEASON_TOP3_LAST10: f"top {_SIGNAL_3_TOP_3} in any of the last {_SIGNAL_3_LAST_10}",
    SEASON_TOP10_LAST5: f"top {_SIGNAL_3_TOP_10} in any of the last {_SIGNAL_3_LAST_5}",
    SEASON_MEAN_LAST5: (
        f"mean rank over the last {_SIGNAL_3_LAST_5} at or under "
        f"{_SIGNAL_3_MEAN_TOP}"
    ),
}


def season_ranks(
    tag: str, name: str | None, ctx: _BulkContext
) -> list[int | None]:
    """Where ``tag`` placed in each of the last 10 seasons, newest first.

    ``None`` for a season it didn't place in — the boards are top-100, so
    absence means "outside the top 100", which is a different thing from
    a bad rank and has to stay distinguishable.

    Season boards identify guilds by **name only** — Wynnpool's
    season-rating payload has no prefix field — so a tag-only match
    silently never fires, and a guild whose full name was never resolved
    cannot place at all. The tag is still checked first in case the shape
    ever gains a prefix.
    """
    folded = name.casefold() if name else None

    def rank_in(board: tuple[wynnpool.LeaderboardEntry, ...]) -> int | None:
        for e in board:
            if tags.matches(e.tag, tag):
                return e.rank
            if folded and e.name and e.name.casefold() == folded:
                return e.rank
        return None

    return [rank_in(board) for board in ctx.season_boards[:_SIGNAL_3_LAST_10]]


def season_rules(ranks: list[int | None]) -> dict[str, bool]:
    """Which of the three placement rules a rank history satisfies."""
    last_10 = ranks[:_SIGNAL_3_LAST_10]
    last_5 = ranks[:_SIGNAL_3_LAST_5]
    placed_5 = [rank for rank in last_5 if rank is not None]

    # The mean rule needs a placement in *every* one of the last five: a
    # guild that placed once at rank 2 and missed four seasons has a
    # brilliant average over the season it turned up to, which is not
    # what "steady record" is meant to mean.
    complete = len(placed_5) == len(last_5) and bool(last_5)

    return {
        SEASON_TOP3_LAST10: any(
            rank is not None and rank <= _SIGNAL_3_TOP_3 for rank in last_10
        ),
        SEASON_TOP10_LAST5: any(
            rank is not None and rank <= _SIGNAL_3_TOP_10 for rank in last_5
        ),
        SEASON_MEAN_LAST5: complete
        and sum(placed_5) / len(placed_5) <= _SIGNAL_3_MEAN_TOP,
    }


def _signal_season_placement(
    tag: str, name: str | None, ctx: _BulkContext
) -> bool:
    """Whether ``tag`` placed well enough in recent seasons — any one rule."""
    if not ctx.season_boards:
        return False
    return any(season_rules(season_ranks(tag, name, ctx)).values())


def _signal_territory_ownership(sustained: bool, season_active: bool) -> bool:
    """Held above the threshold for the window, and only while a season runs.

    The holding question is answered by ``services/territory_history``;
    all that's left here is the season gate, because territory off-season
    costs nothing to keep and proves nothing about war capacity.
    """
    return bool(sustained and season_active)


def _signal_war_count(wars: float | None) -> bool:
    if wars is None:
        return False
    return wars > _SIGNAL_5_MIN_WARS


async def active_notable_overrides() -> dict[str, str]:
    """Every tag currently forced notable: comparison key → as recorded.

    The bulk counterpart to the per-tag check below, for callers weighing
    up a few hundred guilds at once — the roster asking :func:`is_notable`
    per guild would be two queries each for an answer two queries can give
    for all of them.

    Keyed for lookup but carrying the janitor's own spelling, since for a
    guild forced before its first sweep that spelling is the only name we
    have to print.
    """
    now = datetime.now(timezone.utc)
    return {
        tags.normalise(row["subject"]): row["subject"]
        for row in await ForceOverride.filter(kind="notable").values(
            "subject", "expires_at"
        )
        if row["expires_at"] is None or row["expires_at"] > now
    }


async def _has_active_notable_override(tag: str) -> bool:
    now = datetime.now(timezone.utc)
    override = await ForceOverride.filter(
        kind="notable", subject__iexact=tag
    ).first()
    if override is None:
        return False
    return override.expires_at is None or override.expires_at > now
