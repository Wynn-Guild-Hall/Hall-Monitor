"""``~script`` dispatch plus the refresh_notability script's reporting.

The script is the supported way to trigger a notability sweep: doing it
from a second process contends with the bot for the SQLite write lock.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from hall_monitor.discord_bot.cogs.admin.scripts import _loader, refresh_notability
from hall_monitor.services import notability
from hall_monitor.services.notability import RefreshSummary


def _fake_ctx():
    """A Context-like mock whose reply returns an editable message."""
    message = MagicMock()
    message.edit = AsyncMock()
    ctx = MagicMock()
    ctx.reply = AsyncMock(return_value=message)
    return ctx, message


def _edits(message) -> list[str]:
    return [call.kwargs["content"] for call in message.edit.await_args_list]


def test_script_is_discoverable():
    assert "refresh_notability" in set(_loader.discover_scripts())


def test_private_modules_are_not_scripts():
    assert "_loader" not in set(_loader.discover_scripts())


async def test_unknown_script_name_is_reported():
    ctx, _ = _fake_ctx()
    ctx.send = AsyncMock()
    await _loader.run_script("no_such_script", ctx)
    ctx.send.assert_awaited_once()
    assert "unknown script" in ctx.send.await_args.args[0]


async def test_refresh_script_reports_the_summary(monkeypatch):
    async def fake_refresh(*, on_progress=None, exhaustive=False):
        await on_progress(1, 2)
        await on_progress(2, 2)
        return RefreshSummary(evaluated=2, failed=0, notable=1, seconds=12.3)

    monkeypatch.setattr(notability, "refresh_all", fake_refresh)
    monkeypatch.setattr(notability, "is_refreshing", lambda: False)

    ctx, message = _fake_ctx()
    await refresh_notability.main(ctx)

    edits = _edits(message)
    assert "2/2 guilds" in edits[-2]
    assert "12s" in edits[-1]
    assert "2 guilds evaluated, 1 notable" in edits[-1]
    assert "failed" not in edits[-1], "a clean sweep shouldn't mention failures"


async def test_refresh_script_surfaces_failures(monkeypatch):
    async def fake_refresh(*, on_progress=None, exhaustive=False):
        return RefreshSummary(evaluated=8, failed=2, notable=3, seconds=5.0)

    monkeypatch.setattr(notability, "refresh_all", fake_refresh)
    monkeypatch.setattr(notability, "is_refreshing", lambda: False)

    ctx, message = _fake_ctx()
    await refresh_notability.main(ctx)
    assert "2 failed" in _edits(message)[-1]


async def test_refresh_script_throttles_its_edits(monkeypatch):
    """One edit per guild would blow through Discord's edit rate limit, so
    only the final tick is guaranteed to land."""
    async def fake_refresh(*, on_progress=None, exhaustive=False):
        for done in range(1, 51):
            await on_progress(done, 50)
        return RefreshSummary(evaluated=50, failed=0, notable=5, seconds=1.0)

    monkeypatch.setattr(notability, "refresh_all", fake_refresh)
    monkeypatch.setattr(notability, "is_refreshing", lambda: False)

    ctx, message = _fake_ctx()
    await refresh_notability.main(ctx)

    # 50 progress callbacks, but the interval hasn't elapsed: only the
    # completion tick plus the final summary get through.
    assert len(_edits(message)) == 2
    assert "50/50 guilds" in _edits(message)[0]


async def test_refresh_script_declines_when_one_is_running(monkeypatch):
    monkeypatch.setattr(notability, "is_refreshing", lambda: True)
    called = False

    async def fake_refresh(*, on_progress=None, exhaustive=False):
        nonlocal called
        called = True

    monkeypatch.setattr(notability, "refresh_all", fake_refresh)

    ctx, message = _fake_ctx()
    await refresh_notability.main(ctx)

    assert not called, "must not start a second sweep"
    assert "already running" in ctx.reply.await_args.args[0]
    message.edit.assert_not_awaited()


async def test_refresh_script_handles_losing_the_race(monkeypatch):
    """is_refreshing was False at the check but the scheduler got in first."""
    monkeypatch.setattr(notability, "is_refreshing", lambda: False)

    async def fake_refresh(*, on_progress=None, exhaustive=False):
        return None

    monkeypatch.setattr(notability, "refresh_all", fake_refresh)

    ctx, message = _fake_ctx()
    await refresh_notability.main(ctx)
    assert "already running" in _edits(message)[-1]


# --------------------------------------------------------------------------
# notability_breakdown
# --------------------------------------------------------------------------


async def _cache(tag: str, notable: bool, **signals):
    from hall_monitor.db.models import NotabilityCache

    import json as _json

    full = {name: signals.get(name, False) for name in (
        "top25_average_online", "level_100_plus", "season_placement",
        "territory_ownership", "war_count", "force_override",
    )}
    await NotabilityCache.create(
        guild_tag=tag, is_notable=notable, signals_json=_json.dumps(full)
    )


def _table(body: str) -> dict[str, tuple[int, int]]:
    """Parse the code-block table into {signal: (any, only)}.

    Asserting on parsed numbers rather than column positions keeps these
    tests about the counting, not the padding.
    """
    block = body.split("```")[1]
    parsed = {}
    for line in block.strip().splitlines()[1:]:  # skip header
        name, any_count, only_count, skipped = line.split()
        parsed[name] = (int(any_count), int(only_count), int(skipped))
    return parsed


async def test_breakdown_counts_any_versus_sole_cause(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_breakdown

    await _cache("WYNN", True, level_100_plus=True)
    await _cache("AAAB", True, level_100_plus=True)
    await _cache("VETS", True, level_100_plus=True, top25_average_online=True)
    await _cache("SMLL", False)

    ctx, _ = _fake_ctx()
    await notability_breakdown.main(ctx)
    body = ctx.reply.await_args.args[0]

    assert "4 cached, 3 notable" in body
    table = _table(body)
    assert table["level_100_plus"] == (3, 2, 0)
    # top25 corroborates VETS but is never the sole reason for anything.
    assert table["top25_average_online"] == (1, 0, 0)
    assert "sole cause: 2 · multiple: 1 · unexplained: 0" in body


async def test_breakdown_ranks_the_criterion_to_tighten_first(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_breakdown

    await _cache("AAAB", True, season_placement=True)
    for tag in ("WYNN", "AAAC", "AAAD"):
        await _cache(tag, True, level_100_plus=True)

    ctx, _ = _fake_ctx()
    await notability_breakdown.main(ctx)
    body = ctx.reply.await_args.args[0]

    rows = [l for l in body.splitlines() if l.startswith(("level_", "season_"))]
    assert rows[0].startswith("level_100_plus"), "highest sole-cause count leads"


async def test_breakdown_treats_null_as_not_met(db):
    """A skipped signal is `null`, which must not be counted as satisfied."""
    from hall_monitor.db.models import NotabilityCache
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_breakdown

    await NotabilityCache.create(
        guild_tag="VETS",
        is_notable=True,
        signals_json='{"top25_average_online": true, "war_count": null}',
    )
    ctx, _ = _fake_ctx()
    await notability_breakdown.main(ctx)
    body = ctx.reply.await_args.args[0]
    # null == not evaluated: it counts as skipped, never as met.
    assert _table(body)["war_count"] == (0, 0, 1)
    assert _table(body)["top25_average_online"] == (1, 1, 0)
    assert "sole cause: 1" in body


async def test_breakdown_lists_sole_cause_guilds(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_breakdown

    await _cache("WYNN", True, level_100_plus=True)
    await _cache("AAAB", True, level_100_plus=True, war_count=True)

    ctx, _ = _fake_ctx()
    await notability_breakdown.main(ctx, "level_100_plus")
    body = ctx.reply.await_args.args[0]
    assert "**1** guilds are notable on `level_100_plus` alone" in body
    assert "WYNN" in body and "AAAB" not in body


async def test_breakdown_rejects_an_unknown_signal(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_breakdown

    await _cache("WYNN", True, level_100_plus=True)
    ctx, _ = _fake_ctx()
    await notability_breakdown.main(ctx, "wars_lol")
    assert "unknown signal" in ctx.reply.await_args.args[0]


async def test_breakdown_says_so_when_the_cache_is_empty(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_breakdown

    ctx, _ = _fake_ctx()
    await notability_breakdown.main(ctx)
    assert "refresh_notability" in ctx.reply.await_args.args[0]


# --------------------------------------------------------------------------
# notability_table / notable_guilds
# --------------------------------------------------------------------------


async def test_table_and_notable_scripts_are_discoverable():
    found = set(_loader.discover_scripts())
    assert {"notability_table", "notable_guilds"} <= found
    assert "_signal_rows" not in found, "helpers must not be callable as scripts"


async def test_table_writes_a_csv_of_every_guild(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_table

    await _cache("WYNN", False)
    await _cache("VETS", True, level_100_plus=True)

    ctx, _ = _fake_ctx()
    await notability_table.main(ctx)

    body = ctx.reply.await_args.args[0]
    assert "2 guilds, 1 notable" in body

    attachment = ctx.reply.await_args.kwargs["file"]
    text = attachment.fp.getvalue().decode()
    header, *rows = text.strip().splitlines()
    assert header.startswith("tag,notable,top25_average_online,level_100_plus")
    assert rows[0].startswith("VETS,true")   # ordered by tag
    assert rows[1].startswith("WYNN,false")
    assert "level_100_plus" in header


async def test_table_can_restrict_to_notable(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_table

    await _cache("WYNN", False)
    await _cache("VETS", True, war_count=True)

    ctx, _ = _fake_ctx()
    await notability_table.main(ctx, "notable")

    text = ctx.reply.await_args.kwargs["file"].fp.getvalue().decode()
    assert "VETS" in text and "WYNN" not in text
    assert ctx.reply.await_args.kwargs["file"].filename == "notable-guilds.csv"


async def test_table_leaves_an_unevaluated_signal_blank(db):
    """Blank must not read as false — it means nothing asked."""
    from hall_monitor.db.models import NotabilityCache
    from hall_monitor.discord_bot.cogs.admin.scripts import notability_table

    await NotabilityCache.create(
        guild_tag="VETS",
        is_notable=True,
        signals_json='{"level_100_plus": true, "war_count": null}',
    )
    ctx, _ = _fake_ctx()
    await notability_table.main(ctx)

    text = ctx.reply.await_args.kwargs["file"].fp.getvalue().decode()
    row = [l for l in text.splitlines() if l.startswith("VETS")][0]
    columns = text.splitlines()[0].split(",")
    cells = row.split(",")
    assert cells[columns.index("war_count")] == ""
    assert cells[columns.index("level_100_plus")] == "true"
    assert "unevaluated" in ctx.reply.await_args.args[0]


async def test_notable_guilds_lists_only_the_notable(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notable_guilds

    await _cache("WYNN", False)
    await _cache("VETS", True, level_100_plus=True)
    await _cache("AVO", True, war_count=True, level_100_plus=True)

    ctx, _ = _fake_ctx()
    await notable_guilds.main(ctx)
    body = ctx.reply.await_args.args[0]

    assert "**2 notable guilds**" in body
    assert "1 qualify on a single signal" in body
    assert "VETS" in body and "AVO" in body
    assert "WYNN" not in body
    # Sole-cause first, so a threshold change's blast radius reads top-down.
    assert body.index("VETS") < body.index("AVO")


async def test_notable_guilds_splits_across_messages(db):
    """A hundred-odd guilds is past Discord's 2000-char limit."""
    from hall_monitor.discord_bot.cogs.admin.scripts import notable_guilds

    for i in range(120):
        await _cache(f"G{i:03d}", True, level_100_plus=True)

    ctx, _ = _fake_ctx()
    ctx.send = AsyncMock()
    await notable_guilds.main(ctx)

    sent = [ctx.reply.await_args.args[0]] + [
        call.args[0] for call in ctx.send.await_args_list
    ]
    assert len(sent) > 1, "should have split"
    assert all(len(message) <= 2000 for message in sent)
    assert "G119" in "".join(sent)


async def test_notable_guilds_says_so_when_empty(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import notable_guilds

    await _cache("WYNN", False)
    ctx, _ = _fake_ctx()
    await notable_guilds.main(ctx)
    assert "no notable guilds" in ctx.reply.await_args.args[0]


# --------------------------------------------------------------------------
# guild_role
# --------------------------------------------------------------------------


def _ctx_with_guild():
    """A ctx whose guild has no roles and can mint one."""
    ctx, _ = _fake_ctx()
    guild = MagicMock()
    guild.roles = []
    guild.get_role = lambda role_id: None
    minted = MagicMock()
    minted.id = 300
    minted.mention = "<@&300>"
    guild.create_role = AsyncMock(return_value=minted)
    ctx.guild = guild
    return ctx, guild


async def test_guild_role_script_creates_the_role_and_names_the_colour(db, monkeypatch):
    """The point of the script: see the colour without waiting for a chief
    to verify."""
    from hall_monitor.discord_bot.cogs.admin.scripts import guild_role

    async def athena(tag, *, urgent=False):
        return "#7f2727"

    monkeypatch.setattr("hall_monitor.services.athena_colour.lookup", athena)
    ctx, guild = _ctx_with_guild()

    await guild_role.main(ctx, "VETS")

    assert guild.create_role.await_args.kwargs["name"] == "VETS"
    body = ctx.reply.await_args.args[0]
    assert "<@&300>" in body  # mentioned, so the colour renders in-channel
    assert "#9E2E2E" in body  # the Discord-visible variant, not Athena's raw hue


async def test_guild_role_script_flags_the_fallback_colour(db, monkeypatch):
    from hall_monitor.discord_bot.cogs.admin.scripts import guild_role

    async def athena(tag, *, urgent=False):
        return None

    monkeypatch.setattr("hall_monitor.services.athena_colour.lookup", athena)
    ctx, _ = _ctx_with_guild()

    await guild_role.main(ctx, "NEWG")

    assert "Athena doesn't know this guild" in ctx.reply.await_args.args[0]


async def test_guild_watch_script_reports_what_it_found(db, monkeypatch):
    from hall_monitor.discord_bot.cogs.admin.scripts import guild_watch

    async def fake_watch():
        return 4, 1

    monkeypatch.setattr(
        "hall_monitor.services.delegate_registry.refresh_current_guilds", fake_watch
    )
    ctx, message = _fake_ctx()

    await guild_watch.main(ctx)

    body = _edits(message)[-1]
    assert "4 delegate(s) checked, 1 representing a guild they've left" in body
    assert "reconcile" in body, "the poll changes no roles on its own"


async def test_guild_role_script_wants_a_tag(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import guild_role

    ctx, _ = _ctx_with_guild()
    await guild_role.main(ctx)
    assert "usage" in ctx.reply.await_args.args[0]


# --------------------------------------------------------------------------
# standing
# --------------------------------------------------------------------------


def _guild_with(member=None, *, roles=(), my_top_position=10):
    guild = MagicMock()
    guild.roles = list(roles)
    guild.get_role = lambda rid: next((r for r in guild.roles if r.id == rid), None)
    guild.get_member = lambda uid: member if member and uid == member.id else None
    guild.me = MagicMock()
    guild.me.top_role = MagicMock()
    guild.me.top_role.position = my_top_position
    return guild


def _guild_role(role_id=1, name="VETS", position=5):
    role = MagicMock()
    role.id = role_id
    role.name = name
    role.position = position
    role.mention = f"<@&{role_id}>"
    return role


def _standing_member(user_id=1, *, holding=()):
    member = MagicMock()
    member.id = user_id
    member.nick = "Bob [VETS]"
    member.roles = list(holding)
    member.mention = f"<@{user_id}>"
    return member


async def test_standing_script_flags_a_role_it_cannot_manage(db, monkeypatch):
    """The question logs alone couldn't answer: is the guild role above the
    bot, which makes every removal a silent 403."""
    from hall_monitor.db.models import Delegate, GuildRole
    from hall_monitor.discord_bot.cogs.admin.scripts import standing
    from hall_monitor.services import notability as notability_service

    await Delegate.create(
        mc_uuid="u", discord_user_id=1, guild_tag="VETS", current_guild_tag="ANO"
    )
    role = _guild_role(position=99)  # dragged above the bot
    await GuildRole.create(guild_tag="VETS", discord_role_id=role.id)

    async def notable(tag):
        return True

    monkeypatch.setattr(notability_service, "is_notable", notable)
    member = _standing_member(1, holding=[role])
    ctx, _ = _fake_ctx()
    ctx.guild = _guild_with(member, roles=[role], my_top_position=10)

    await standing.main(ctx, "<@1>")
    body = ctx.reply.await_args.args[0]

    assert "above me, so I cannot touch it" in body
    assert "standing:** `external`" in body  # represents VETS, seen in ANO
    assert "watch last saw:** `ANO`" in body


async def test_standing_script_names_the_guild_each_slot_belongs_to(db, monkeypatch):
    """Slots survive a `~force guild` — the rows never move — so somebody
    repointed back to VETS still holds ANO's ownership row. Printed as a
    bare `ownership` it reads exactly like the roster being wrong."""
    from hall_monitor.db.models import Delegate, GuildContact
    from hall_monitor.discord_bot.cogs.admin.scripts import standing
    from hall_monitor.services import notability as notability_service

    delegate = await Delegate.create(
        mc_uuid="u", discord_user_id=1, guild_tag="VETS", current_guild_tag="VETS"
    )
    await GuildContact.create(guild_tag="ANO", role="ownership", delegate=delegate)

    async def notable(tag):
        return True

    monkeypatch.setattr(notability_service, "is_notable", notable)
    ctx, _ = _fake_ctx()
    ctx.guild = _guild_with(_standing_member(1))

    await standing.main(ctx, "<@1>")
    body = ctx.reply.await_args.args[0]

    assert "`ANO` ownership" in body
    assert "withheld" in body, "and says why the roster shows it unclaimed"


async def test_standing_script_needs_a_member(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import standing

    ctx, _ = _fake_ctx()
    ctx.guild = _guild_with(None)
    await standing.main(ctx, "<@404>")
    assert "isn't a member" in ctx.reply.await_args.args[0]
