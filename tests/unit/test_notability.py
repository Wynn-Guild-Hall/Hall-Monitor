"""Coverage for each of the 6 notability signals + force-override precedence."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from hall_monitor.db.models import Delegate, ForceOverride, NotabilityCache
from hall_monitor.external import wynncraft, wynnpool
from hall_monitor.services import notability
from hall_monitor.services.notability import (
    _BulkContext,
    _signal_level_100_plus,
    _signal_season_placement,
    _signal_territory_ownership,
    _signal_top25_avg_online,
    _signal_war_count,
    is_notable,
    refresh_all,
)


@pytest.fixture(autouse=True)
def fresh_refresh_lock():
    """Give each test its own sweep lock.

    ``notability._refresh_lock`` is module state, and pytest-asyncio runs
    every test on a new event loop. A lock left in any state by one test —
    or bound to a loop that has since closed — makes the next test's
    ``acquire`` wait on a future nothing will ever resolve.
    """
    notability._refresh_lock = asyncio.Lock()
    yield


def _lb(rank: int, tag: str, name: str = "n", value: float | None = None):
    return wynnpool.LeaderboardEntry(rank=rank, name=name, tag=tag, value=value)


def _ctx(**overrides) -> _BulkContext:
    defaults = dict(
        tag_to_name={},
        avg_online=(),
        guild_level=(),
        season_boards=(),
        current_season_active=False,
    )
    defaults.update(overrides)
    return _BulkContext(**defaults)


def _guild(*, level=1, territories=0, wars=0) -> wynncraft.Guild:
    return wynncraft.Guild(
        uuid="u",
        name="G",
        prefix="G",
        level=level,
        territories=territories,
        members=(),
        banner=None,
        wars=wars,
    )


# --------------------------------------------------------------------------
# Signal 1 — top-25 average online
# --------------------------------------------------------------------------


def test_signal_1_true_when_in_top_25():
    ctx = _ctx(avg_online=(_lb(3, "VETS"), _lb(4, "OTHR")))
    assert _signal_top25_avg_online("VETS", ctx) is True


def test_signal_1_false_when_below_top_25():
    ctx = _ctx(avg_online=(_lb(26, "VETS"),))
    assert _signal_top25_avg_online("VETS", ctx) is False


def test_signal_1_false_when_absent():
    assert _signal_top25_avg_online("VETS", _ctx()) is False


# --------------------------------------------------------------------------
# Signal 2 — level 100+ on the guildLevel leaderboard
# --------------------------------------------------------------------------


def test_signal_2_true_when_level_100_plus():
    ctx = _ctx(guild_level=(_lb(1, "VETS", value=130),))
    assert _signal_level_100_plus("VETS", ctx) is True


def test_signal_2_false_when_level_below_100():
    ctx = _ctx(guild_level=(_lb(1, "VETS", value=42),))
    assert _signal_level_100_plus("VETS", ctx) is False


def test_signal_2_treats_missing_value_as_qualifying():
    """If Wynnpool doesn't publish the level, presence on the leaderboard
    is enough — the leaderboard is already level-ranked."""
    ctx = _ctx(guild_level=(_lb(1, "VETS", value=None),))
    assert _signal_level_100_plus("VETS", ctx) is True


# --------------------------------------------------------------------------
# Signal 3 — season placement (three sub-conditions)
# --------------------------------------------------------------------------


def _season_lb(rank: int, name: str = "Returners"):
    """A season-board row as Wynnpool actually ships it: named, **untagged**.

    Building these with a tag is what let a tag-only match pass its tests
    while never once firing in production.
    """
    return wynnpool.LeaderboardEntry(rank=rank, name=name, tag=None, value=1.0)


def test_signal_3_top_3_in_any_of_last_10_seasons():
    seasons = tuple(
        (_season_lb(rank=3),) if i == 7 else (_season_lb(rank=80),)
        for i in range(10)
    )
    ctx = _ctx(season_boards=seasons)
    assert _signal_season_placement("VETS", "Returners", ctx) is True


def test_signal_3_top_10_in_any_of_last_5_seasons():
    seasons = tuple(
        (_season_lb(rank=8),) if i == 2 else (_season_lb(rank=50),)
        for i in range(5)
    )
    ctx = _ctx(season_boards=seasons)
    assert _signal_season_placement("VETS", "Returners", ctx) is True


def test_signal_3_mean_rank_across_last_5_below_25():
    seasons = tuple((_season_lb(rank=r),) for r in (11, 15, 20, 25, 30))
    ctx = _ctx(season_boards=seasons)
    assert _signal_season_placement("VETS", "Returners", ctx) is True


def test_signal_3_false_when_never_placed():
    seasons = tuple((_season_lb(rank=100),) for _ in range(10))
    ctx = _ctx(season_boards=seasons)
    assert _signal_season_placement("VETS", "Returners", ctx) is False


def test_signal_3_false_when_no_seasons():
    assert _signal_season_placement("VETS", "Returners", _ctx()) is False


def test_signal_3_matches_name_case_insensitively():
    ctx = _ctx(season_boards=((_season_lb(rank=1, name="RETURNERS"),),))
    assert _signal_season_placement("VETS", "Returners", ctx) is True


def test_signal_3_ignores_a_different_guild_with_a_good_rank():
    ctx = _ctx(season_boards=((_season_lb(rank=1, name="Sequoia"),),))
    assert _signal_season_placement("VETS", "Returners", ctx) is False


def test_signal_3_false_when_the_guild_name_is_unknown():
    """No name and no tag on the board means nothing to match on — the
    signal has to stay false rather than match the first row."""
    ctx = _ctx(season_boards=((_season_lb(rank=1),),))
    assert _signal_season_placement("VETS", None, ctx) is False


def test_signal_3_still_matches_on_tag_if_wynnpool_adds_one():
    ctx = _ctx(season_boards=((_lb(rank=1, tag="VETS"),),))
    assert _signal_season_placement("VETS", None, ctx) is True


# --------------------------------------------------------------------------
# Signal 4 — territory ownership + active-season gate
# --------------------------------------------------------------------------


def test_signal_4_true_when_over_20_and_season_active():
    assert _signal_territory_ownership(_guild(territories=25), True) is True


def test_signal_4_false_when_no_active_season():
    """Twenty-five territories don't count off-season."""
    assert _signal_territory_ownership(_guild(territories=25), False) is False


def test_signal_4_false_at_boundary():
    assert _signal_territory_ownership(_guild(territories=20), True) is False


def test_signal_4_false_when_guild_missing():
    assert _signal_territory_ownership(None, True) is False


# --------------------------------------------------------------------------
# Signal 5 — war count > 50 000
# --------------------------------------------------------------------------


def test_signal_5_true_above_threshold():
    assert _signal_war_count(_guild(wars=50_001)) is True


def test_signal_5_false_at_threshold():
    assert _signal_war_count(_guild(wars=50_000)) is False


def test_signal_5_false_when_wars_null():
    assert _signal_war_count(_guild(wars=None)) is False


def test_signal_5_false_when_guild_missing():
    assert _signal_war_count(None) is False


# --------------------------------------------------------------------------
# Signal 6 — force override (DB-backed)
# --------------------------------------------------------------------------


async def test_force_override_beats_signals(db):
    await ForceOverride.create(kind="notable", subject="VETS", expires_at=None)
    assert await is_notable("VETS") is True


async def test_expired_force_override_ignored(db):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await ForceOverride.create(kind="notable", subject="VETS", expires_at=past)
    await NotabilityCache.create(
        guild_tag="VETS", is_notable=False, signals_json="{}"
    )
    assert await is_notable("VETS") is False


async def test_force_override_ignored_for_other_kinds(db):
    await ForceOverride.create(kind="guild", subject="VETS", expires_at=None)
    await NotabilityCache.create(
        guild_tag="VETS", is_notable=False, signals_json="{}"
    )
    assert await is_notable("VETS") is False


# --------------------------------------------------------------------------
# Cache reads and refresh_all wiring
# --------------------------------------------------------------------------


async def test_is_notable_reads_cache(db):
    """Cache hit skips the expensive path — no API calls needed."""
    await NotabilityCache.create(
        guild_tag="VETS", is_notable=True, signals_json="{}"
    )
    # No httpx_mock — if _load_context ran, this would raise.
    assert await is_notable("VETS") is True


async def test_refresh_all_writes_cache_for_all_candidates(db, httpx_mock, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    # Bulk leaderboards — one guild on each so we can assert union coverage.
    httpx_mock.add_response(
        url="https://api.wynnpool.com/leaderboard/guild-average-online",
        json={"1": {"name": "Wynncraft Veterans", "prefix": "VETS", "averageOnline": 40}},
    )
    httpx_mock.add_response(
        url="https://api.wynnpool.com/leaderboard/guildLevel",
        json={"1": {"name": "Wynncraft Veterans", "prefix": "VETS", "level": 130}},
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/guild/seasons",
        json={},  # no historical seasons → no season boards needed
    )
    # Delegate row expands candidate set.
    await Delegate.create(
        mc_uuid="u", discord_user_id=1, guild_tag="DELEG"
    )
    # Force override expands candidate set (with a distinct tag).
    await ForceOverride.create(kind="notable", subject="OVRD", expires_at=None)

    # Only DELEG needs a per-guild fetch. VETS is settled by the
    # leaderboards and OVRD by its override, so mocking their guild
    # endpoints would leave pytest-httpx holding unrequested responses —
    # which is the assertion that the fetch really is skipped.
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/guild/prefix/DELEG",
        status_code=404,
    )

    await refresh_all()

    cached_tags = {r.guild_tag for r in await NotabilityCache.all()}
    assert cached_tags == {"VETS", "DELEG", "OVRD"}

    # VETS is notable on the leaderboards alone; the two signals that would
    # have cost a Wynncraft call are recorded as unevaluated, not false.
    vets = await NotabilityCache.get(guild_tag="VETS")
    assert vets.is_notable is True
    vets_signals = json.loads(vets.signals_json)
    assert vets_signals["top25_average_online"] is True
    assert vets_signals["territory_ownership"] is None
    assert vets_signals["war_count"] is None

    # OVRD has no real signals but has a force override — notable, and it
    # short-circuits before the fetch just the same.
    ovrd = await NotabilityCache.get(guild_tag="OVRD")
    assert ovrd.is_notable is True
    assert json.loads(ovrd.signals_json)["war_count"] is None

    # DELEG qualifies on nothing, so it does pay for the lookup — and the
    # 404 leaves the per-guild signals evaluated-and-false rather than None.
    deleg = await NotabilityCache.get(guild_tag="DELEG")
    assert deleg.is_notable is False
    assert json.loads(deleg.signals_json)["war_count"] is False


async def test_refresh_all_survives_untagged_season_entries(db, httpx_mock, monkeypatch):
    """The season boards are the one source of tag-less entries. Letting a
    None into the candidate set made `sorted(tags)` raise, and because it
    raises *before* the per-tag try/except, the whole hourly refresh died —
    every run, silently, until someone read the logs."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    httpx_mock.add_response(
        url="https://api.wynnpool.com/leaderboard/guild-average-online",
        json={"1": {"name": "Returners", "prefix": "VETS", "averageOnline": 40}},
    )
    httpx_mock.add_response(
        url="https://api.wynnpool.com/leaderboard/guildLevel", json={}
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/guild/seasons",
        json={"31": {"startDate": "2020-01-01T00:00:00Z", "endDate": "2020-02-01T00:00:00Z"}},
    )
    # Season boards: named, no prefix — exactly what Wynnpool returns.
    httpx_mock.add_response(
        url="https://api.wynnpool.com/leaderboard/season-rating/31",
        json={
            "season": 31,
            "ranking": [
                {"rank": 1, "guild_uuid": "x", "guild_name": "Returners", "rating": 999},
                {"rank": 2, "guild_uuid": "y", "guild_name": "Sequoia", "rating": 500},
            ],
        },
    )
    # No Wynncraft guild mock: VETS is settled by the leaderboards before
    # the per-guild fetch would happen.

    await refresh_all()

    cached = {r.guild_tag for r in await NotabilityCache.all()}
    assert cached == {"VETS"}, "a None tag must not reach the candidate set"
    # Rank 1 last season → signal 3, matched by name since the board has no tag.
    vets = await NotabilityCache.get(guild_tag="VETS")
    assert json.loads(vets.signals_json)["season_placement"] is True


async def test_is_notable_slow_path_populates_cache(db, httpx_mock, monkeypatch):
    """Cache miss triggers a full evaluation and stores the result."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    httpx_mock.add_response(
        url="https://api.wynnpool.com/leaderboard/guild-average-online", json=[]
    )
    httpx_mock.add_response(
        url="https://api.wynnpool.com/leaderboard/guildLevel", json=[]
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/guild/seasons", json={}
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/guild/prefix/NEW", status_code=404
    )

    assert await is_notable("NEW") is False
    assert await NotabilityCache.get_or_none(guild_tag="NEW") is not None


# --------------------------------------------------------------------------
# Sweep bookkeeping — single-flight, progress, summary
# --------------------------------------------------------------------------


def _empty_boards(httpx_mock):
    httpx_mock.add_response(
        url="https://api.wynnpool.com/leaderboard/guild-average-online", json={}
    )
    httpx_mock.add_response(
        url="https://api.wynnpool.com/leaderboard/guildLevel", json={}
    )
    httpx_mock.add_response(url="https://api.wynncraft.com/v3/guild/seasons", json={})


async def test_refresh_reports_progress_and_a_summary(db, httpx_mock, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _empty_boards(httpx_mock)
    await ForceOverride.create(kind="notable", subject="WYNN", expires_at=None)
    await ForceOverride.create(kind="notable", subject="VETS", expires_at=None)

    seen: list[tuple[int, int]] = []

    async def on_progress(done, total):
        seen.append((done, total))

    summary = await refresh_all(on_progress=on_progress)

    assert seen == [(1, 2), (2, 2)]
    assert summary is not None
    assert (summary.evaluated, summary.notable, summary.failed) == (2, 2, 0)
    assert summary.seconds >= 0


async def test_refresh_is_single_flight(db, httpx_mock):
    """The scheduler firing while an operator's `~script` sweep is mid-run
    would double the per-guild API load, so the later trigger is dropped
    rather than queued behind it.

    ``httpx_mock`` is requested with nothing registered on purpose: it
    blocks outbound HTTP, so a broken guard fails immediately instead of
    reaching the real APIs and looking like a hang.
    """
    await notability._refresh_lock.acquire()  # stand in for a sweep in flight
    try:
        assert notability.is_refreshing()
        assert await refresh_all() is None
    finally:
        notability._refresh_lock.release()
    assert not notability.is_refreshing()


async def test_refresh_survives_a_broken_progress_callback(db, httpx_mock, monkeypatch):
    """Reporting is a nicety — it must not cost us the sweep."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _empty_boards(httpx_mock)
    await ForceOverride.create(kind="notable", subject="WYNN", expires_at=None)

    async def on_progress(done, total):
        raise RuntimeError("discord fell over")

    summary = await refresh_all(on_progress=on_progress)
    assert summary is not None and summary.evaluated == 1
    assert await NotabilityCache.get_or_none(guild_tag="WYNN") is not None


async def test_refresh_counts_a_failure_without_aborting(db, httpx_mock, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _empty_boards(httpx_mock)
    await ForceOverride.create(kind="notable", subject="WYNN", expires_at=None)
    await ForceOverride.create(kind="notable", subject="VETS", expires_at=None)

    calls: list[str] = []
    real = notability._evaluate_and_cache

    async def flaky(tag, context, *, exhaustive=False):
        calls.append(tag)
        if tag == "VETS":
            raise RuntimeError("boom")
        return await real(tag, context)

    monkeypatch.setattr(notability, "_evaluate_and_cache", flaky)
    summary = await refresh_all()

    assert calls == ["VETS", "WYNN"], "a failure must not stop the sweep"
    assert (summary.evaluated, summary.failed, summary.notable) == (1, 1, 1)
