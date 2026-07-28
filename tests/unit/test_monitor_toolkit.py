"""Stage 15's monitor commands: `~force expel`, `~force rep`, `~manage`,
and the guild-watch staleness the brief's 48h guarantee rests on."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from hall_monitor.db.models import (
    Delegate,
    ExpelBan,
    ExpelMotion,
    ForceOverride,
    GuildContact,
    GuildRole,
)
from hall_monitor.discord_bot.cogs.force import expel as force_expel, rep as force_rep
from hall_monitor.services import (
    athena_colour,
    contacts,
    delegate_registry,
    expel,
    expel_motion,
    major_guilds,
)

CONTACT_ROLE_IDS = {"events": 200, "housing": 201, "warring": 202, "ownership": 203}


def _role(role_id, name):
    role = MagicMock()
    role.id = role_id
    role.name = name
    role.colour = MagicMock()
    role.mentionable = True
    role.members = []
    role.edit = AsyncMock()
    role.delete = AsyncMock()
    return role


class FakeGuild:
    def __init__(self, roles=()):
        self.id = 1
        self.roles = [
            _role(role_id, f"{name} contact")
            for name, role_id in CONTACT_ROLE_IDS.items()
        ] + list(roles)
        self.members = {}
        self.create_role = AsyncMock(side_effect=self._create)

    async def _create(self, *, name, colour, mentionable, hoist, reason):
        role = _role(900 + len(self.roles), name)
        self.roles.append(role)
        return role

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    def get_channel(self, channel_id):
        return None

    def get_member(self, user_id):
        return self.members.get(user_id)

    def add_member(self, user_id, *, holding=()):
        member = MagicMock()
        member.id = user_id
        member.mention = f"<@{user_id}>"
        member.nick = None
        member.name = "Tester"
        member.guild = self
        member.roles = list(holding)
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        member.kick = AsyncMock()
        member.edit = AsyncMock()
        self.members[user_id] = member
        return member


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for name, role_id in CONTACT_ROLE_IDS.items():
        monkeypatch.setattr(
            f"hall_monitor.services.contacts.settings.{name}_contact_role_id", role_id
        )
    monkeypatch.setattr(
        "hall_monitor.services.guild_roles.settings.delegate_role_id", 100
    )
    athena_colour.reset_cache()

    async def unknown(guild_tag, *, urgent=False):
        return None

    monkeypatch.setattr(athena_colour, "lookup", unknown)
    yield
    athena_colour.reset_cache()


@pytest.fixture
def major(monkeypatch):
    async def always(tag):
        return True

    monkeypatch.setattr(major_guilds, "is_major", always)


@pytest.fixture
def guild():
    return FakeGuild([_role(100, "Guild Hall Delegate")])


def _ctx(guild):
    ctx = MagicMock()
    ctx.guild = guild
    ctx.author = MagicMock()
    ctx.author.id = 999
    ctx.bot = MagicMock()
    ctx.reply = AsyncMock()
    return ctx


async def _delegate(user_id, tag, *, uuid=None, username=None):
    return await Delegate.create(
        mc_uuid=uuid or f"uuid-{user_id}",
        mc_username=username,
        discord_user_id=user_id,
        guild_tag=tag,
        current_guild_tag=tag,
    )


class _Guild:
    """A Wynncraft guild reference, as `get_player_guild` returns one."""

    def __init__(self, prefix, rank="CHIEF"):
        self.prefix = prefix
        self.rank = rank
        self.name = prefix


# --------------------------------------------------------------------------
# ~force expel
# --------------------------------------------------------------------------


async def test_force_expel_bars_and_removes(db, guild, major):
    target = await _delegate(1, "OTHR")
    guild.add_member(1)
    await GuildContact.create(guild_tag="OTHR", role="ownership", delegate=target)

    reply = await force_expel.bar(_ctx(guild), "OTHR")

    assert await expel.is_banned("OTHR")
    guild.members[1].kick.assert_awaited()
    assert (await Delegate.get(id=target.id)).left_at is not None
    assert not await GuildContact.filter(guild_tag="OTHR").exists()
    assert "barred from the Hall" in reply and "1 representative" in reply


async def test_force_expel_against_an_empty_guild_reads_as_success(db, guild):
    """Barring a guild before it ever arrives is an ordinary thing to do,
    and has to read as done rather than as a command that didn't work."""
    reply = await force_expel.bar(_ctx(guild), "NEVR")

    assert await expel.is_banned("NEVR")
    assert "nobody was removed" in reply


async def test_force_expel_refuses_a_guild_already_barred(db, guild):
    await ExpelBan.create(guild_tag="OTHR", reason="earlier")

    reply = await force_expel.bar(_ctx(guild), "othr")

    assert "already barred" in reply
    assert await ExpelBan.all().count() == 1


async def test_force_expel_closes_an_open_motion(db, guild, major):
    """Leaving it open would put live buttons under a vote about a guild
    that has already gone — and one that could still "carry" later."""
    await _delegate(1, "VETS")
    guild.add_member(1)
    await _delegate(2, "OTHR")
    guild.add_member(2)
    motion = await ExpelMotion.create(
        guild_tag="OTHR", opened_by_discord_user_id=1, opened_by_guild_tag="VETS"
    )

    reply = await force_expel.bar(_ctx(guild), "OTHR")

    settled = await ExpelMotion.get(id=motion.id)
    assert settled.state == expel_motion.SUPERSEDED
    assert settled.resolved_at is not None
    assert "Closed 1 open motion" in reply


async def test_a_superseded_motion_reads_as_neither_carried_nor_lapsed(db):
    motion = ExpelMotion(
        guild_tag="OTHR",
        opened_by_discord_user_id=1,
        opened_by_guild_tag="VETS",
        state=expel_motion.SUPERSEDED,
        created_at=datetime.now(timezone.utc),
    )

    body = expel_motion.render_resolved(
        motion, expel_motion.Tally(electorate=5, yay=1, nay=1), None
    )

    assert "barred from the Hall by a monitor" in body
    assert "carried" not in body and "lapsed" not in body


async def test_unforce_expel_lifts_the_ban_and_nothing_else(db, guild, major):
    delegate = await _delegate(1, "OTHR")
    guild.add_member(1)
    await force_expel.bar(_ctx(guild), "OTHR")

    reply = await force_expel.unbar(_ctx(guild), "OTHR")

    assert not await expel.is_banned("OTHR")
    assert (await Delegate.get(id=delegate.id)).left_at is not None
    assert "verify again from scratch" in reply


async def test_unforce_expel_on_an_unbanned_guild_says_so(db, guild):
    assert "nothing to lift" in await force_expel.unbar(_ctx(guild), "OTHR")


# --------------------------------------------------------------------------
# ~force rep
# --------------------------------------------------------------------------


@pytest.fixture
def wynncraft_says(monkeypatch):
    """Control what Wynncraft reports about a UUID's guild and rank."""
    answer = {}

    async def fake(mc_uuid, *, urgent=False):
        return answer.get(mc_uuid)

    monkeypatch.setattr(
        "hall_monitor.external.wynncraft.get_player_guild", fake
    )
    return answer


async def test_force_rep_repoints_a_confirmed_chief(db, guild, major, wynncraft_says):
    delegate = await _delegate(1, "VETS", uuid="u1")
    member = guild.add_member(1)
    await GuildContact.create(guild_tag="VETS", role="ownership", delegate=delegate)
    wynncraft_says["u1"] = _Guild("ANO")

    reply = await force_rep.repoint(_ctx(guild), member, "ANO")

    moved = await Delegate.get(id=delegate.id)
    assert moved.guild_tag == "ANO" and moved.current_guild_tag == "ANO"
    assert not await GuildContact.filter(guild_tag="VETS").exists()
    member.kick.assert_not_awaited(), "they're staying — the kick is for losing a slot"
    assert "now represents `ANO`" in reply and "gave up" in reply


async def test_force_rep_refuses_when_wynncraft_disagrees(db, guild, wynncraft_says):
    """The one place a human asserts something the game is the authority
    on, so it checks rather than trusting the operator."""
    delegate = await _delegate(1, "VETS", uuid="u1")
    member = guild.add_member(1)
    wynncraft_says["u1"] = _Guild("SEQ")

    reply = await force_rep.repoint(_ctx(guild), member, "ANO")

    assert (await Delegate.get(id=delegate.id)).guild_tag == "VETS", "untouched"
    assert "has" in reply and "`SEQ`" in reply


async def test_force_rep_refuses_an_ordinary_member(db, guild, wynncraft_says):
    delegate = await _delegate(1, "VETS", uuid="u1")
    member = guild.add_member(1)
    wynncraft_says["u1"] = _Guild("ANO", rank="RECRUITER")

    reply = await force_rep.repoint(_ctx(guild), member, "ANO")

    assert (await Delegate.get(id=delegate.id)).guild_tag == "VETS"
    assert "chief or owner of any guild" in reply


async def test_force_rep_drops_a_force_guild_override(db, guild, major, wynncraft_says):
    """`~force guild` sits in front of the row, so leaving one would make
    this command appear to have done nothing at all."""
    await _delegate(1, "VETS", uuid="u1")
    member = guild.add_member(1)
    await delegate_registry.set_forced_guild(1, "SEQ", None)
    wynncraft_says["u1"] = _Guild("ANO")

    reply = await force_rep.repoint(_ctx(guild), member, "ANO")

    assert not await ForceOverride.filter(kind="guild", subject="1").exists()
    assert "Dropped their `~force guild` override" in reply


async def test_force_rep_refuses_a_barred_guild(db, guild, wynncraft_says):
    await _delegate(1, "VETS", uuid="u1")
    member = guild.add_member(1)
    await ExpelBan.create(guild_tag="ANO", reason="voted out")
    wynncraft_says["u1"] = _Guild("ANO")

    assert "barred from the Hall" in await force_rep.repoint(_ctx(guild), member, "ANO")
    assert (await Delegate.get(discord_user_id=1)).guild_tag == "VETS"


async def test_force_rep_on_a_non_delegate_says_so(db, guild):
    member = guild.add_member(1)

    assert "isn't a registered representative" in await force_rep.repoint(
        _ctx(guild), member, "ANO"
    )


async def test_force_rep_reports_the_end_state_it_applied(db, guild, major, wynncraft_says):
    """Stage 10's structural fix: a command whose effect only shows up at
    the next reconcile is indistinguishable from one that did nothing."""
    await _delegate(1, "VETS", uuid="u1")
    member = guild.add_member(1)
    await GuildRole.create(guild_tag="VETS", discord_role_id=555)
    wynncraft_says["u1"] = _Guild("ANO")

    reply = await force_rep.repoint(_ctx(guild), member, "ANO")

    assert "Now standing `delegate`, wearing `ANO`" in reply


# --------------------------------------------------------------------------
# Vacating slots without a kick
# --------------------------------------------------------------------------


async def test_vacating_holdings_takes_the_rows_and_the_roles_but_not_the_member(
    db, guild
):
    delegate = await _delegate(1, "VETS")
    member = guild.add_member(1, holding=[guild.get_role(203)])
    await GuildContact.create(guild_tag="VETS", role="ownership", delegate=delegate)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    given_up = await contacts.vacate_holdings(delegate, "VETS", discord_guild=guild)

    assert given_up == ["events", "ownership"]
    assert not await GuildContact.filter(delegate_id=delegate.id).exists()
    member.kick.assert_not_awaited()
    assert 203 in {
        role.id for call in member.remove_roles.await_args_list for role in call.args
    }


async def test_vacating_leaves_another_guilds_slots_alone(db, guild):
    delegate = await _delegate(1, "VETS")
    guild.add_member(1)
    await GuildContact.create(guild_tag="ANO", role="events", delegate=delegate)

    assert await contacts.vacate_holdings(delegate, "VETS", discord_guild=guild) == []
    assert await GuildContact.filter(guild_tag="ANO").exists()


# --------------------------------------------------------------------------
# The guild watch, and whether it's actually running
# --------------------------------------------------------------------------


async def test_a_successful_check_stamps_the_timestamp(db, monkeypatch):
    delegate = await _delegate(1, "VETS", uuid="u1")

    async def fake(mc_uuid, *, urgent=False):
        return _Guild("VETS")

    monkeypatch.setattr("hall_monitor.external.wynncraft.get_player_guild", fake)
    await delegate_registry.refresh_current_guilds()

    assert (await Delegate.get(id=delegate.id)).current_guild_checked_at is not None


async def test_a_failed_check_keeps_the_guild_and_does_not_stamp(db, monkeypatch):
    """The whole reason for the column. Keeping the last known guild is
    right per-call and unbounded across calls — sustained failures would
    freeze every delegate silently, and this is what shows it."""
    import httpx

    delegate = await _delegate(1, "VETS", uuid="u1")

    async def fails(mc_uuid, *, urgent=False):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr("hall_monitor.external.wynncraft.get_player_guild", fails)
    await delegate_registry.refresh_current_guilds()

    row = await Delegate.get(id=delegate.id)
    assert row.current_guild_tag == "VETS", "last known value kept"
    assert row.current_guild_checked_at is None, "but the clock did not move"


async def test_stale_delegates_are_the_ones_past_the_window(db):
    old = await _delegate(1, "VETS", uuid="u1")
    old.current_guild_checked_at = datetime.now(timezone.utc) - timedelta(hours=50)
    await old.save()
    fresh = await _delegate(2, "ANO", uuid="u2")
    fresh.current_guild_checked_at = datetime.now(timezone.utc)
    await fresh.save()

    stale = await delegate_registry.stale_delegates()

    assert [one.id for one in stale] == [old.id]


async def test_a_delegate_who_just_verified_is_not_stale(db):
    """A row written moments ago simply hasn't had its first sweep yet;
    reporting it would make a fresh verification look like a fault."""
    await _delegate(1, "VETS", uuid="u1")

    assert await delegate_registry.stale_delegates() == []


async def test_a_delegate_never_checked_goes_stale_eventually(db):
    delegate = await _delegate(1, "VETS", uuid="u1")
    delegate.joined_at = datetime.now(timezone.utc) - timedelta(days=5)
    await delegate.save()

    assert [one.id for one in await delegate_registry.stale_delegates()] == [
        delegate.id
    ]


async def test_a_departed_delegate_is_never_chased(db):
    delegate = await _delegate(1, "VETS", uuid="u1")
    delegate.joined_at = datetime.now(timezone.utc) - timedelta(days=5)
    delegate.left_at = datetime.now(timezone.utc)
    await delegate.save()

    assert await delegate_registry.stale_delegates() == []


async def test_delegate_status_names_the_overdue(db):
    from hall_monitor.discord_bot.cogs.admin.scripts import delegate_status

    overdue = await _delegate(1, "VETS", uuid="u1", username="Holidaze")
    overdue.current_guild_checked_at = datetime.now(timezone.utc) - timedelta(hours=50)
    await overdue.save()

    ctx = MagicMock()
    ctx.reply = AsyncMock()
    await delegate_status.main(ctx)

    body = ctx.reply.await_args.args[0]
    assert "overdue: **1**" in body
    assert "Holidaze" in body


async def test_delegate_status_says_when_there_is_nothing_to_chase(db, monkeypatch):
    await _delegate(1, "VETS", uuid="u1")
    from hall_monitor.discord_bot.cogs.admin.scripts import delegate_status

    ctx = MagicMock()
    ctx.reply = AsyncMock()
    await delegate_status.main(ctx)

    assert "nothing to chase" in ctx.reply.await_args.args[0]


# --------------------------------------------------------------------------
# ~manage
# --------------------------------------------------------------------------


async def test_reload_cogs_names_the_ones_that_failed(db):
    """A partial reload leaves the process running some old code and some
    new, and which is which decides whether the next test proves anything."""
    from discord.ext import commands as discord_commands

    from hall_monitor.discord_bot.cogs.admin import manage as manage_module

    bot = MagicMock()

    async def reload(name):
        # Fully qualified: `endswith("major")` also catches
        # `scripts.refresh_major`, which would make this assert two.
        if name.endswith("cogs.force.major"):
            raise RuntimeError("boom")
        if name.endswith("_loader"):
            raise discord_commands.ExtensionNotLoaded(name)

    bot.reload_extension = AsyncMock(side_effect=reload)
    cog = manage_module.Manage(bot)
    ctx = _ctx(None)

    await cog.reload_cogs.callback(cog, ctx)

    reply = ctx.reply.await_args.args[0]
    assert "1 failed and kept their old code" in reply
    assert "`major`" in reply


async def test_reload_cogs_is_quiet_when_everything_reloads(db):
    from hall_monitor.discord_bot.cogs.admin import manage as manage_module

    bot = MagicMock()
    bot.reload_extension = AsyncMock()
    cog = manage_module.Manage(bot)
    ctx = _ctx(None)

    await cog.reload_cogs.callback(cog, ctx)

    reply = ctx.reply.await_args.args[0]
    assert "reloaded" in reply and "failed" not in reply


async def test_shutdown_drains_the_roster_before_exiting(db, monkeypatch):
    """The roster sync is debounced and fire-and-forget, so exiting a
    second after a `~force` would drop a redraw nothing else will make."""
    from hall_monitor.discord_bot.cogs.admin import manage as manage_module

    order = []

    async def drain():
        order.append("drained")

    async def close():
        order.append("closed")

    monkeypatch.setattr(
        manage_module.roster, "wait_for_pending_sync", drain
    )
    monkeypatch.setattr(manage_module.os, "kill", lambda pid, sig: order.append("sigterm"))

    bot = MagicMock()
    bot.close = close
    cog = manage_module.Manage(bot)
    ctx = _ctx(None)

    await cog.shutdown.callback(cog, ctx)

    assert order == ["drained", "closed", "sigterm"]
    assert "restart" in ctx.reply.await_args.args[0]
