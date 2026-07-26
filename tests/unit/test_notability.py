"""Coverage for each of the 6 notability signals + force-override precedence."""

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


def test_signal_3_top_3_in_any_of_last_10_seasons():
    seasons = tuple(
        (_lb(rank=3, tag="VETS"),) if i == 7 else (_lb(rank=80, tag="VETS"),)
        for i in range(10)
    )
    ctx = _ctx(season_boards=seasons)
    assert _signal_season_placement("VETS", ctx) is True


def test_signal_3_top_10_in_any_of_last_5_seasons():
    seasons = tuple(
        (_lb(rank=8, tag="VETS"),) if i == 2 else (_lb(rank=50, tag="VETS"),)
        for i in range(5)
    )
    ctx = _ctx(season_boards=seasons)
    assert _signal_season_placement("VETS", ctx) is True


def test_signal_3_mean_rank_across_last_5_below_25():
    seasons = tuple((_lb(rank=r, tag="VETS"),) for r in (11, 15, 20, 25, 30))
    ctx = _ctx(season_boards=seasons)
    assert _signal_season_placement("VETS", ctx) is True


def test_signal_3_false_when_never_placed():
    seasons = tuple((_lb(rank=100, tag="VETS"),) for _ in range(10))
    ctx = _ctx(season_boards=seasons)
    assert _signal_season_placement("VETS", ctx) is False


def test_signal_3_false_when_no_seasons():
    assert _signal_season_placement("VETS", _ctx()) is False


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

    # For each per-guild fetch, wynncraft.get_guild(name) is called; DELEG has
    # no name mapping so it falls through to get_guild_by_prefix. Mock both.
    for name in ("Wynncraft Veterans",):
        httpx_mock.add_response(
            url=f"https://api.wynncraft.com/v3/guild/{name}",
            json={
                "uuid": "u", "name": name, "prefix": "VETS",
                "level": 130, "territories": 25, "wars": 60000,
                "members": {},
            },
        )
    for tag in ("DELEG", "OVRD"):
        httpx_mock.add_response(
            url=f"https://api.wynncraft.com/v3/guild/prefix/{tag}",
            status_code=404,
        )

    await refresh_all()

    cached_tags = {r.guild_tag for r in await NotabilityCache.all()}
    assert cached_tags == {"VETS", "DELEG", "OVRD"}
    # VETS should be notable (level + war count + override-less).
    vets = await NotabilityCache.get(guild_tag="VETS")
    assert vets.is_notable is True
    # OVRD has no signals but has a force override — notable.
    ovrd = await NotabilityCache.get(guild_tag="OVRD")
    assert ovrd.is_notable is True
    # DELEG has neither — not notable.
    deleg = await NotabilityCache.get(guild_tag="DELEG")
    assert deleg.is_notable is False


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
