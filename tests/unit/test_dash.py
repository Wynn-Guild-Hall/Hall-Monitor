"""Stage 16: `~dash`, against a declared set of keys.

The interesting half is the refusals. A contact who can't invent a key
has to be able to discover the ones that exist and told which command
each takes — otherwise the constraint that makes the Hallway page
possible just makes the command unusable.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from hall_monitor.db.models import DashKV, Delegate
from hall_monitor.discord_bot.cogs.general import dash as dash_cog
from hall_monitor.services import dash, dash_schema

# The live schema is deliberately **empty** — nothing renders these yet,
# so a key that could be filled in but is displayed nowhere would invite
# guilds to write into a place nobody is looking. These tests declare
# their own, which is the right coupling anyway: they're about the
# machinery, not about which questions the Hall happens to ask this week.
A_BOOL = "a_bool_key"
A_SCALAR = "a_scalar_key"

FIXTURE_KEYS = {
    A_BOOL: dash_schema.Key(A_BOOL, dash_schema.BOOL, "A yes/no question"),
    A_SCALAR: dash_schema.Key(A_SCALAR, dash_schema.SCALAR, "A question with text"),
}


@pytest.fixture(autouse=True)
def schema(monkeypatch):
    monkeypatch.setattr(dash_schema, "KEYS", FIXTURE_KEYS)


def _ctx(user_id: int = 1):
    ctx = MagicMock()
    ctx.author.id = user_id
    ctx.reply = AsyncMock()
    return ctx


def _said(ctx) -> str:
    return ctx.reply.await_args.args[0]


async def _delegate(user_id: int = 1, tag: str = "VETS") -> Delegate:
    return await Delegate.create(
        mc_uuid=f"uuid-{user_id}",
        discord_user_id=user_id,
        guild_tag=tag,
        current_guild_tag=tag,
    )


async def _run(cog, command, ctx, **kwargs):
    await getattr(cog, command).callback(cog, ctx, **kwargs)


@pytest.fixture
def cog():
    return dash_cog.Dash(MagicMock())


# --------------------------------------------------------------------------
# The schema itself
# --------------------------------------------------------------------------


def test_every_declared_key_is_self_consistent():
    """The dict is keyed by name and the name is what everything else
    looks up, so a mismatch would make a key unreachable."""
    for name, key in dash_schema.KEYS.items():
        assert key.name == name
        assert key.kind in {dash_schema.BOOL, dash_schema.SCALAR}
        assert key.description, f"{name} needs a description — the listing shows it"
        assert key.max_length > 0


def test_keys_match_case_insensitively():
    """A human types these. `A_Bool_Key` and `a_bool_key` are the same
    question, and refusing the first would be a puzzle, not a rule."""
    assert dash_schema.get(A_BOOL.upper()).name == A_BOOL
    assert dash_schema.get(f"  {A_BOOL.title()}  ").name == A_BOOL


def test_an_undeclared_key_is_unknown():
    with pytest.raises(dash_schema.UnknownKey):
        dash_schema.get("made_up_key")


def test_requiring_the_wrong_kind_says_which_command_it_takes():
    with pytest.raises(dash_schema.WrongKind) as caught:
        dash_schema.require(A_SCALAR, dash_schema.BOOL)

    assert caught.value.key.command == "set"


def test_a_scalar_over_the_limit_is_refused_not_truncated():
    """A value silently cut at the limit is worse than one that didn't
    save — nothing tells the author the end of their sentence has gone."""
    key = dash_schema.get(A_SCALAR)
    with pytest.raises(dash_schema.BadValue):
        dash_schema.clean_scalar(key, "x" * (key.max_length + 1))


@pytest.mark.parametrize("raw", ["yes", "Y", "true", "ON", "1", "open"])
def test_truthy_spellings(raw):
    assert dash_schema.parse_bool(raw) is True


@pytest.mark.parametrize("raw", ["no", "N", "false", "OFF", "0", "closed"])
def test_falsey_spellings(raw):
    assert dash_schema.parse_bool(raw) is False


def test_a_value_that_is_neither_is_refused():
    with pytest.raises(dash_schema.BadValue):
        dash_schema.parse_bool("maybe")


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


async def test_unset_is_the_absence_of_a_row(db):
    """A guild that never answered and one that answered then cleared it
    have to read identically — that's what `unset` promises."""
    key = dash_schema.get(A_BOOL)
    assert await dash.values_for("VETS") == {}

    await dash.write("VETS", key, True)
    assert await dash.values_for("VETS") == {A_BOOL: True}

    assert await dash.clear("VETS", key) is True
    assert await dash.values_for("VETS") == {}


async def test_clearing_nothing_reports_it(db):
    assert await dash.clear("VETS", dash_schema.get(A_BOOL)) is False


async def test_values_are_per_guild(db):
    await dash.write("VETS", dash_schema.get(A_BOOL), True)
    await dash.write("ANO", dash_schema.get(A_BOOL), False)

    assert (await dash.values_for("VETS"))[A_BOOL] is True
    assert (await dash.values_for("ANO"))[A_BOOL] is False


async def test_a_retired_key_is_ignored_rather_than_deleted(db):
    """Retiring a key stays reversible: put it back and the guilds that
    had answered still have their answers."""
    await DashKV.create(guild_tag="VETS", key="was_a_key_once", value_json='"x"')

    assert await dash.values_for("VETS") == {}
    assert await DashKV.filter(key="was_a_key_once").exists()


async def test_unreadable_json_reads_as_unset(db, caplog):
    """A page should render a guild with one bad row, not fail on it."""
    await DashKV.create(guild_tag="VETS", key=A_BOOL, value_json="{not json")

    assert await dash.values_for("VETS") == {}


# --------------------------------------------------------------------------
# The commands
# --------------------------------------------------------------------------


async def test_toggle_writes_a_bool(db, cog):
    await _delegate()
    ctx = _ctx()

    await _run(cog, "toggle", ctx, key=A_BOOL, value="yes")

    assert (await dash.values_for("VETS"))[A_BOOL] is True
    assert "is now **yes**" in _said(ctx)


async def test_set_writes_a_scalar(db, cog):
    await _delegate()
    ctx = _ctx()

    await _run(cog, "set_", ctx, key=A_SCALAR, value="  DM an officer  ")

    assert (await dash.values_for("VETS"))[A_SCALAR] == "DM an officer"


async def test_unset_clears_and_says_so(db, cog):
    await _delegate()
    await dash.write("VETS", dash_schema.get(A_BOOL), True)
    ctx = _ctx()

    await _run(cog, "unset", ctx, key=A_BOOL)

    assert await dash.values_for("VETS") == {}
    assert "unset again" in _said(ctx)


async def test_unsetting_something_already_unset_says_so(db, cog):
    await _delegate()
    ctx = _ctx()

    await _run(cog, "unset", ctx, key=A_BOOL)

    assert "already unset" in _said(ctx)


async def test_toggle_rejects_a_value_that_is_neither(db, cog):
    await _delegate()
    ctx = _ctx()

    await _run(cog, "toggle", ctx, key=A_BOOL, value="maybe")

    assert "isn't a yes or a no" in _said(ctx)
    assert await dash.values_for("VETS") == {}


async def test_set_rejects_an_over_long_value_with_the_numbers(db, cog):
    await _delegate()
    ctx = _ctx()
    limit = dash_schema.get(A_SCALAR).max_length

    await _run(cog, "set_", ctx, key=A_SCALAR, value="x" * (limit + 5))

    assert f"{limit + 5} characters" in _said(ctx)
    assert str(limit) in _said(ctx)
    assert await dash.values_for("VETS") == {}


async def test_set_with_nothing_points_at_unset(db, cog):
    await _delegate()
    ctx = _ctx()

    await _run(cog, "set_", ctx, key=A_SCALAR, value="")

    assert "`~dash unset`" in _said(ctx)


async def test_values_follow_the_guild_they_represent_not_their_row(db, cog):
    """`~force guild` repoints who somebody speaks for, and using the row
    would let a member repointed to ANO carry on editing VETS's page."""
    from hall_monitor.services import delegate_registry

    await _delegate()
    await delegate_registry.set_forced_guild(1, "ANO", None)
    ctx = _ctx()

    await _run(cog, "toggle", ctx, key=A_BOOL, value="yes")

    assert await dash.values_for("VETS") == {}
    assert (await dash.values_for("ANO"))[A_BOOL] is True


# --------------------------------------------------------------------------
# Discovery, and the refusals that need it
# --------------------------------------------------------------------------


async def test_the_listing_shows_every_key_and_its_value(db, cog):
    await _delegate()
    await dash.write("VETS", dash_schema.get(A_BOOL), False)
    ctx = _ctx()

    await _run(cog, "dash", ctx)

    body = _said(ctx)
    for key in dash_schema.KEYS.values():
        assert f"`{key.name}`" in body
        assert key.description in body
    assert "_unset_" in body, "an unanswered key reads as unset"
    assert f"`{A_BOOL}`: no" in body


def test_an_unknown_key_is_refused_with_the_ones_that_exist():
    """Somebody who can't invent a key has no other way to find out what
    they may set, so the refusal has to carry the list."""
    body = dash_cog.unknown_key("made_up_key")

    assert "made_up_key" in body
    for key in dash_schema.KEYS.values():
        assert key.name in body
        assert f"~dash {key.command}" in body


def test_a_wrong_kind_names_the_command_it_does_take():
    exc = dash_schema.WrongKind(dash_schema.get(A_SCALAR), dash_schema.BOOL)

    body = dash_cog.wrong_kind(exc)

    assert f"`{A_SCALAR}` takes `~dash set`" in body
    assert "not `~dash toggle`" in body


async def test_the_error_handler_turns_an_unknown_key_into_the_listing(db, cog):
    ctx = _ctx()

    await cog.cog_command_error(ctx, dash_schema.UnknownKey("made_up_key"))

    assert "isn't one of the Hall's questions" in _said(ctx)


async def test_the_error_handler_leaves_other_errors_to_the_global_one(db, cog):
    ctx = _ctx()

    await cog.cog_command_error(ctx, RuntimeError("boom"))

    ctx.reply.assert_not_awaited()


async def test_somebody_with_no_delegate_row_is_told(db, cog):
    """Staff pass the contact gate by nesting, so a janitor reaches here
    and has no guild to write against."""
    ctx = _ctx()

    with pytest.raises(dash_cog.NoGuild):
        await dash_cog.speaking_for(ctx)


async def test_a_departed_delegate_cannot_edit(db, cog):
    from datetime import datetime, timezone

    delegate = await _delegate()
    delegate.left_at = datetime.now(timezone.utc)
    await delegate.save()

    with pytest.raises(dash_cog.NoGuild):
        await dash_cog.speaking_for(_ctx())


# --------------------------------------------------------------------------
# The empty schema — what actually ships
# --------------------------------------------------------------------------


def test_the_live_schema_is_empty():
    """Deliberate, and this test is the reminder of why. Nothing renders
    a guild's answers yet, and a key that can be filled in but is
    displayed nowhere invites guilds to write into a place nobody is
    looking — worse than a missing feature, because it looks like a
    working one. A key arrives when its consumer does; deleting this test
    is part of adding the first.
    """
    import importlib

    assert importlib.reload(dash_schema).KEYS == {}


async def test_the_listing_says_nothing_is_asked_rather_than_showing_none(
    db, cog, monkeypatch
):
    """An empty list under a header reads as something having gone
    wrong."""
    monkeypatch.setattr(dash_schema, "KEYS", {})
    await _delegate()
    ctx = _ctx()

    await _run(cog, "dash", ctx)

    body = _said(ctx)
    assert "isn't asking anything yet" in body
    assert "dashboard**" not in body


def test_an_unknown_key_with_no_schema_doesnt_offer_an_empty_list(monkeypatch):
    monkeypatch.setattr(dash_schema, "KEYS", {})

    body = dash_cog.unknown_key("anything")

    assert "isn't asking anything yet" in body
    assert not body.rstrip().endswith("These are:")
