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

    The memoised bulk context is module state too, and so is the season
    board cache: left in place, one test's leaderboards would answer
    another's lookup and its registered mocks would go unrequested.
    """
    notability._refresh_lock = asyncio.Lock()
    notability.reset_context_memo()
    notability.reset_season_cache()
    yield
    notability.reset_context_memo()
    notability.reset_season_cache()


def _lb(rank: int, tag: str, name: str = "n", value: float | None = None):
    return wynnpool.LeaderboardEntry(rank=rank, name=name, tag=tag, value=value)


def _ctx(**overrides) -> _BulkContext:
    defaults = dict(
        tag_to_name={},
        avg_online=(),
        guild_level=(),
        wars=(),
        territories=(),
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
    assert _signal_territory_ownership(25, True) is True


def test_signal_4_false_when_no_active_season():
    """Twenty-five territories don't count off-season."""
    assert _signal_territory_ownership(25, False) is False


def test_signal_4_false_at_boundary():
    assert _signal_territory_ownership(20, True) is False


def test_signal_4_false_when_absent_from_the_board():
    """Off the territories board means below its floor, which is below 20."""
    assert _signal_territory_ownership(None, True) is False


# --------------------------------------------------------------------------
# Signal 5 — war count > 50 000
# --------------------------------------------------------------------------


def test_signal_5_true_above_threshold():
    assert _signal_war_count(50_001) is True


def test_signal_5_false_at_threshold():
    assert _signal_war_count(50_000) is False


def test_signal_5_false_when_absent_from_the_board():
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
    _boards(httpx_mock, boards={
        "guild-average-online": {
            "1": {"name": "Returners", "prefix": "VETS", "averageOnline": 40}
        },
        "guildLevel": {"1": {"name": "Returners", "prefix": "VETS", "level": 130}},
        # Each board's *floor* must sit below the threshold, or absence
        # from it proves nothing and we fall back to per-guild lookups.
        "guildWars": {
            "1": {"name": "Avicia", "prefix": "AVO", "wars": 225394},
            "2": {"name": "Bottom", "prefix": "BTMW", "wars": 4108},
        },
        "guildTerritories": {
            "1": {"name": "Aequitas", "prefix": "Aeq", "territories": 97},
            "2": {"name": "Bottom", "prefix": "BTMT", "territories": 0},
        },
    }, names={"DELEG": "Delegated", "OVRD": None})
    await Delegate.create(mc_uuid="u", discord_user_id=1, guild_tag="DELEG")
    await ForceOverride.create(kind="notable", subject="OVRD", expires_at=None)

    await refresh_all()

    # Boards contribute candidates alongside delegates and overrides — and
    # no per-guild request is registered, so any would fail the test.
    cached = {r.guild_tag for r in await NotabilityCache.all()}
    assert cached == {"VETS", "AVO", "BTMW", "Aeq", "BTMT", "DELEG", "OVRD"}

    vets = await NotabilityCache.get(guild_tag="VETS")
    assert vets.is_notable is True
    signals = json.loads(vets.signals_json)
    assert signals["top25_average_online"] is True
    # Every signal is evaluated now; none are left null.
    assert None not in signals.values()
    assert signals["war_count"] is False

    avo = await NotabilityCache.get(guild_tag="AVO")
    assert avo.is_notable is True
    assert json.loads(avo.signals_json)["war_count"] is True

    ovrd = await NotabilityCache.get(guild_tag="OVRD")
    assert ovrd.is_notable is True
    deleg = await NotabilityCache.get(guild_tag="DELEG")
    assert deleg.is_notable is False


async def test_a_guild_off_every_board_still_learns_its_name(db, httpx_mock, monkeypatch):
    """The roster prints the name, and `VETS` is on no board carrying a
    prefix — so it read as `**VETS** (`VETS`)` until this was asked for."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _empty_boards(httpx_mock, names={"VETS": "Returners"})
    await ForceOverride.create(kind="notable", subject="VETS", expires_at=None)

    await refresh_all()

    assert (await NotabilityCache.get(guild_tag="VETS")).guild_name == "Returners"


async def test_a_name_already_known_is_never_re_fetched(db, httpx_mock, monkeypatch):
    """One prefix lookup per guild ever. `names={}` registers no response,
    so any request at all fails the test."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _empty_boards(httpx_mock)
    await ForceOverride.create(kind="notable", subject="VETS", expires_at=None)
    await NotabilityCache.create(
        guild_tag="VETS", is_notable=True, signals_json="{}", guild_name="Returners"
    )

    await refresh_all()

    assert (await NotabilityCache.get(guild_tag="VETS")).guild_name == "Returners"


async def test_a_tag_nothing_knows_keeps_its_tag_as_a_name(db, httpx_mock, monkeypatch):
    """An invented tag somebody forced. It costs one request a sweep and
    must not stop the guild being evaluated."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _empty_boards(httpx_mock, names={"ZZZZ": None})
    await ForceOverride.create(kind="notable", subject="ZZZZ", expires_at=None)

    await refresh_all()

    cached = await NotabilityCache.get(guild_tag="ZZZZ")
    assert cached.guild_name is None and cached.is_notable is True


async def test_refresh_falls_back_per_guild_when_a_board_stops_covering(
    db, httpx_mock, monkeypatch
):
    """If the wars board's floor climbs above 50 000, absence from it stops
    proving anything and the guild has to be asked directly."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _boards(httpx_mock, boards={
        "guildLevel": {"1": {"name": "Returners", "prefix": "VETS", "level": 42}},
        # Floor of 60k is above our 50k threshold — no longer decisive.
        "guildWars": {
            "1": {"name": "Avicia", "prefix": "AVO", "wars": 225394},
            "2": {"name": "Other", "prefix": "OTHR", "wars": 60000},
        },
    })
    httpx_mock.add_response(
        url="https://api.wynnpool.com/guild/Returners",
        json={"name": "Returners", "prefix": "VETS", "wars": 51000, "territories": 0},
    )

    await refresh_all()

    vets = await NotabilityCache.get(guild_tag="VETS")
    assert json.loads(vets.signals_json)["war_count"] is True
    assert vets.is_notable is True


async def test_refresh_all_survives_untagged_season_entries(db, httpx_mock, monkeypatch):
    """The season boards are the one source of tag-less entries. Letting a
    None into the candidate set made `sorted(tags)` raise, and because it
    raises *before* the per-tag try/except, the whole hourly refresh died —
    every run, silently, until someone read the logs."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _boards(
        httpx_mock,
        boards={
            "guild-average-online": {
                "1": {"name": "Returners", "prefix": "VETS", "averageOnline": 40}
            }
        },
        seasons={
            "31": {
                "startDate": "2020-01-01T00:00:00Z",
                "endDate": "2020-02-01T00:00:00Z",
            }
        },
        season_boards={
            31: {
                "season": 31,
                "ranking": [
                    {"rank": 1, "guild_uuid": "x", "guild_name": "Returners", "rating": 999},
                    {"rank": 2, "guild_uuid": "y", "guild_name": "Sequoia", "rating": 500},
                ],
            }
        },
    )

    await refresh_all()

    cached = {r.guild_tag for r in await NotabilityCache.all()}
    assert cached == {"VETS"}, "a None tag must not reach the candidate set"
    vets = await NotabilityCache.get(guild_tag="VETS")
    assert json.loads(vets.signals_json)["season_placement"] is True


async def test_is_notable_slow_path_populates_cache(db, httpx_mock, monkeypatch):
    """Cache miss triggers a full evaluation and stores the result."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _boards(httpx_mock)

    assert await is_notable("NEW") is False
    assert await NotabilityCache.get_or_none(guild_tag="NEW") is not None


# --------------------------------------------------------------------------
# Sweep bookkeeping — single-flight, progress, summary
# --------------------------------------------------------------------------


def _boards(httpx_mock, **overrides):
    """Register every board `_load_context` fetches, empty unless overridden.

    pytest-httpx fails on an unregistered request, so this doubles as the
    assertion that the sweep asks for exactly these and nothing per-guild.
    """
    boards = {
        "guild-average-online": {},
        "guildLevel": {},
        "guildWars": {},
        "guildTerritories": {},
        **{name: {} for name in notability._CANDIDATE_BOARDS},
    }
    boards.update(overrides.pop("boards", {}))
    for name, payload in boards.items():
        httpx_mock.add_response(
            url=f"https://api.wynnpool.com/leaderboard/{name}", json=payload
        )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/guild/seasons",
        json=overrides.pop("seasons", {}),
    )
    for number, payload in (overrides.pop("season_boards", {}) or {}).items():
        httpx_mock.add_response(
            url=f"https://api.wynnpool.com/leaderboard/season-rating/{number}",
            json=payload,
        )
    # A candidate off every board has no name, so the sweep asks Wynncraft
    # for one. Map the tag to a name, or to `None` for a tag nothing knows.
    for tag, name in (overrides.pop("names", {}) or {}).items():
        if name is None:
            httpx_mock.add_response(
                url=f"https://api.wynncraft.com/v3/guild/prefix/{tag}", status_code=404
            )
            continue
        httpx_mock.add_response(
            url=f"https://api.wynncraft.com/v3/guild/prefix/{tag}",
            json={
                "uuid": f"uuid-{tag}",
                "name": name,
                "prefix": tag,
                "level": 1,
                "territories": 0,
                "members": {},
            },
        )
    assert not overrides, f"unused: {sorted(overrides)}"


def _empty_boards(httpx_mock, **overrides):
    _boards(httpx_mock, **overrides)


async def test_refresh_reports_progress_and_a_summary(db, httpx_mock, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _empty_boards(httpx_mock, names={"WYNN": "Wynn Admins", "VETS": "Returners"})
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
    _empty_boards(httpx_mock, names={"WYNN": "Wynn Admins"})
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
    _empty_boards(httpx_mock, names={"WYNN": "Wynn Admins", "VETS": "Returners"})
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


async def test_a_cache_miss_reuses_the_last_sweep_s_leaderboards(db, httpx_mock, monkeypatch):
    """The miss path runs inside `/api/verify`, which a player is waiting
    on in Minecraft. Rebuilding the context there was twenty-odd
    sequential fetches; picolimbo blocks that client's connection for the
    duration, so slow reads back as disconnected."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _boards(httpx_mock)          # one set of boards, for the sweep only
    await refresh_all()

    # No further boards registered: a second load would fail on an
    # unmatched request, which is the assertion.
    assert await is_notable("NEWG") is False
    assert await NotabilityCache.get_or_none(guild_tag="NEWG") is not None


async def test_the_memo_expires(db, httpx_mock, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    monkeypatch.setattr(notability, "_CONTEXT_TTL_S", -1)  # instantly stale
    _boards(httpx_mock)
    await refresh_all()
    _boards(httpx_mock)          # a stale memo must fetch again
    assert await is_notable("NEWG") is False


async def test_a_force_override_matches_the_tag_whatever_its_case(db):
    """A janitor types the tag by hand; Wynncraft's capitalisation is
    whatever it is. An override keyed differently to the lookup is an
    override that does nothing."""
    await ForceOverride.create(kind="notable", subject="vets", expires_at=None)
    assert await is_notable("VETS") is True
    assert await is_notable("Vets") is True


async def test_signals_match_a_differently_cased_board_entry(db):
    ctx = _ctx(
        avg_online=(_lb(3, "VETS"),),
        guild_level=(_lb(1, "VETS", value=130),),
        wars=(_lb(1, "VETS", value=99_999),),
        tag_to_name={"VETS": "Returners"},
        folded_to_name={"vets": "Returners"},
    )
    assert _signal_top25_avg_online("vets", ctx) is True
    assert _signal_level_100_plus("vets", ctx) is True
    assert ctx.name_for("vEtS") == "Returners"


# --------------------------------------------------------------------------
# Strength — ordering guilds against each other, for the emote budget
# --------------------------------------------------------------------------


def test_matched_signal_count_dominates():
    """Notability is a yes/no, but a guild qualifying four ways is more
    securely part of the Hall than one scraping in on a leaderboard."""
    many = notability.strength(
        {"level_100_plus": True, "war_count": True}, {"guild_level": 101, "wars": 50_001}
    )
    one = notability.strength({"level_100_plus": True}, {"guild_level": 140})

    assert many > one


def test_a_tie_on_count_falls_to_the_numbers():
    stronger = notability.strength({"level_100_plus": True}, {"guild_level": 140})
    weaker = notability.strength({"level_100_plus": True}, {"guild_level": 101})

    assert stronger > weaker


def test_rank_based_signals_are_inverted():
    """Rank 1 is the strongest, not the weakest — the one place a raw
    value sorts exactly backwards."""
    first = notability.strength(
        {"top25_average_online": True}, {"average_online_rank": 1}
    )
    twentieth = notability.strength(
        {"top25_average_online": True}, {"average_online_rank": 20}
    )

    assert first > twentieth


def test_an_unmatched_signal_contributes_nothing():
    """No credit for being *nearly* good at something you didn't qualify
    on — otherwise a guild's near-misses could outrank a real signal."""
    near_miss = notability.strength(
        {"level_100_plus": False, "war_count": True},
        {"guild_level": 99, "wars": 50_001},
    )
    assert near_miss[1 + notability.SIGNAL_ORDER.index("level_100_plus")] == 0.0


async def test_strength_by_tag_reads_the_cache(db):
    await NotabilityCache.create(
        guild_tag="VETS", is_notable=True,
        signals_json=json.dumps({"level_100_plus": True}),
        metrics_json=json.dumps({"guild_level": 130}),
    )

    by_tag = await notability.strength_by_tag()

    assert by_tag["vets"][0] == 1.0
    assert by_tag["vets"][1 + notability.SIGNAL_ORDER.index("level_100_plus")] == 130.0


async def test_a_row_with_unreadable_json_is_skipped_not_fatal(db):
    await NotabilityCache.create(
        guild_tag="BAD", is_notable=True, signals_json="{not json",
        metrics_json="{}",
    )
    await NotabilityCache.create(
        guild_tag="OK", is_notable=True,
        signals_json=json.dumps({"war_count": True}),
        metrics_json=json.dumps({"wars": 60_000}),
    )

    by_tag = await notability.strength_by_tag()

    assert "bad" not in by_tag and "ok" in by_tag


async def test_the_sweep_records_the_numbers_behind_the_signals(db, httpx_mock, monkeypatch):
    """The booleans say whether a guild qualifies; these say by how much,
    and nothing can rank guilds without them."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _boards(httpx_mock, boards={
        "guildLevel": {"1": {"name": "Returners", "prefix": "VETS", "level": 130}},
    })

    await refresh_all()

    metrics = json.loads((await NotabilityCache.get(guild_tag="VETS")).metrics_json)
    assert metrics["guild_level"] == 130


# --------------------------------------------------------------------------
# Staying inside Wynnpool's per-IP rate limit
# --------------------------------------------------------------------------


async def test_a_rate_limited_season_board_costs_that_board_not_the_sweep(
    db, httpx_mock, monkeypatch
):
    """A 429 on one season board used to abort the whole refresh — the
    gather had no `return_exceptions` — and `~script refresh_notability`
    came back with "that broke on my end"."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    _boards(
        httpx_mock,
        boards={"guildLevel": {"1": {"name": "Returners", "prefix": "VETS", "level": 130}}},
        seasons={"32": {"startDate": "2020-01-01T00:00:00Z", "endDate": "2020-02-01T00:00:00Z"}},
    )
    # Both attempts 429 — `retry_429` waits out the pause and tries once.
    for _ in range(2):
        httpx_mock.add_response(
            url="https://api.wynnpool.com/leaderboard/season-rating/32", status_code=429
        )

    summary = await refresh_all()

    assert summary is not None and summary.failed == 0
    assert (await NotabilityCache.get(guild_tag="VETS")).is_notable is True


async def test_a_finished_season_board_is_fetched_once(db, httpx_mock, monkeypatch):
    """Season 32's ratings will never change again. Re-reading them every
    hour is most of the sweep's request budget for nothing."""
    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.settings.wynncraft_api_token", ""
    )
    finished = {"32": {"startDate": "2020-01-01T00:00:00Z", "endDate": "2020-02-01T00:00:00Z"}}
    _boards(httpx_mock, seasons=finished, season_boards={32: {"ranking": []}})

    await refresh_all()
    # Every board registered above has now been requested once. A second
    # sweep re-reads the live ones; season 32 is over, so it must not be
    # asked for again — there is no second response registered for it.
    _boards(httpx_mock, seasons=finished)
    await refresh_all()
