"""`~force guild` — repointing which guild a member represents.

The override doesn't say "they're playing over there", it says "they
speak for this guild now", and everything keys off that single answer:
the tag on their nickname, the colour they wear, which slots they may
hold, and whose major-guild status decides their standing.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from hall_monitor.db.models import Delegate, ForceOverride, GuildRole
from hall_monitor.services import delegate_registry


async def _delegate(user_id=1, *, tag="VETS", currently=None):
    return await Delegate.create(
        mc_uuid=f"uuid-{user_id}",
        discord_user_id=user_id,
        guild_tag=tag,
        current_guild_tag=currently,
    )


def _in(**kwargs) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**kwargs)


async def test_the_override_repoints_who_they_represent(db):
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    assert await delegate_registry.represented_guild(delegate) == "VETS"

    await delegate_registry.set_forced_guild(1, "ANO", _in(days=30))

    assert await delegate_registry.represented_guild(delegate) == "ANO"


async def test_a_forced_representative_is_never_external(db):
    """The watch seeing a different guild is the exact situation the
    janitor overrode — relegating them for it would make the override
    useless in the case it exists for."""
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "ANO", _in(days=30))

    assert await delegate_registry.is_external(delegate) is False
    assert (
        await delegate_registry.standing(delegate, major=True)
        == delegate_registry.DELEGATE
    )


async def test_the_override_can_undo_a_wrong_external(db):
    """The other common case: a rep mid-transfer, or an alt showing the
    wrong guild. Forcing the guild they already represent puts them back."""
    delegate = await _delegate(1, tag="VETS", currently="OTHR")
    assert await delegate_registry.is_external(delegate) is True

    await delegate_registry.set_forced_guild(1, "VETS", _in(days=30))

    assert await delegate_registry.is_external(delegate) is False
    assert (
        await delegate_registry.standing(delegate, major=True)
        == delegate_registry.DELEGATE
    )


async def test_drifting_off_unforced_is_still_external(db):
    delegate = await _delegate(1, tag="VETS", currently="OTHR")
    assert (
        await delegate_registry.standing(delegate, major=True)
        == delegate_registry.EXTERNAL
    )
    assert await delegate_registry.represented_guild(delegate) == "VETS"


async def test_the_override_beats_the_watch_not_the_other_way(db):
    """The watch rewrites `current_guild_tag` hourly, so an override stored
    in that column would survive exactly one sweep."""
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "ANO", None)

    delegate.current_guild_tag = "VETS"  # a watch pass writing what it sees
    await delegate.save()

    assert await delegate_registry.represented_guild(delegate) == "ANO"


async def test_an_expired_override_stops_counting(db):
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "ANO", _in(days=-1))

    assert await delegate_registry.represented_guild(delegate) == "VETS"
    assert await ForceOverride.filter(kind="guild").count() == 1, (
        "left in place so a janitor can see what ran out"
    )


async def test_a_permanent_override_has_no_expiry(db):
    await _delegate(1)
    await delegate_registry.set_forced_guild(1, "ANO", None)
    assert await delegate_registry.forced_guild(1) == "ANO"


async def test_re_forcing_replaces_rather_than_stacking(db):
    """Two rows would mean the second `~force guild` silently did nothing."""
    await _delegate(1)
    await delegate_registry.set_forced_guild(1, "ANO", _in(days=30))
    await delegate_registry.set_forced_guild(1, "THRD", _in(days=60))

    assert await ForceOverride.filter(kind="guild", subject="1").count() == 1
    assert await delegate_registry.forced_guild(1) == "THRD"


async def test_clearing_goes_back_to_the_verified_guild(db):
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "ANO", None)

    assert await delegate_registry.clear_forced_guild(1) == 1

    assert await delegate_registry.represented_guild(delegate) == "VETS"
    assert await delegate_registry.is_external(delegate) is False


async def test_clearing_nothing_reports_nothing(db):
    assert await delegate_registry.clear_forced_guild(999) == 0


async def test_the_override_is_per_member(db):
    theirs = await _delegate(2, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "ANO", None)

    assert await delegate_registry.represented_guild(theirs) == "VETS"


async def test_the_forced_tag_folds_case(db):
    """`~force guild @them vets` has to mean the guild cached as VETS."""
    delegate = await _delegate(1, tag="VETS", currently="OTHR")
    await delegate_registry.set_forced_guild(1, "vets", None)

    assert await delegate_registry.is_external(delegate) is False


# --------------------------------------------------------------------------
# Applying it on the spot — `apply_now`
# --------------------------------------------------------------------------


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


class _Guild:
    def __init__(self, roles):
        self.roles = list(roles)
        self.members = {}
        self.create_role = AsyncMock(side_effect=self._create)

    async def _create(self, *, name, colour, mentionable, hoist, reason):
        role = _role(900 + len(self.roles), name)
        self.roles.append(role)
        return role

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    def get_member(self, user_id):
        return self.members.get(user_id)


def _member(guild, user_id, *, holding=()):
    member = MagicMock()
    member.id = user_id
    member.guild = guild
    member.nick = None
    member.name = "Tester"
    member.roles = list(holding)
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.edit = AsyncMock()
    guild.members[user_id] = member
    return member


async def test_apply_now_settles_the_guild_they_moved_to(db, monkeypatch):
    """The bug the walkthrough caught: `apply_now` settled the row's guild,
    which after the repoint is the one pass that no longer contains them —
    so a member forced to ANO got no ANO role and no error either."""
    from hall_monitor.discord_bot.cogs.force import guild as force_guild
    from hall_monitor.services import athena_colour, guild_roles, major_guilds

    monkeypatch.setattr(
        "hall_monitor.services.guild_roles.settings.delegate_role_id", 100
    )
    athena_colour.reset_cache()

    async def unknown(guild_tag, *, urgent=False):
        return None

    async def major(tag):
        return True

    monkeypatch.setattr(athena_colour, "lookup", unknown)
    monkeypatch.setattr(major_guilds, "is_major", major)

    vets = _role(1, "VETS")
    delegate_role = _role(100, "Guild Hall Delegate")
    guild = _Guild([vets, delegate_role])
    await GuildRole.create(guild_tag="VETS", discord_role_id=1)
    await _delegate(1, tag="VETS", currently="VETS")
    member = _member(guild, 1, holding=[vets])
    await delegate_registry.set_forced_guild(1, "ANO", None)

    ctx = MagicMock()
    ctx.guild = guild
    ctx.author = "janitor"
    applied = await force_guild.apply_now(ctx, member)

    assert guild.create_role.await_args.kwargs["name"] == "ANO", "minted on demand"
    # The reply says what happened, so a no-op can't read as success.
    assert applied.changed is True
    assert "Now standing `delegate`, wearing `ANO`" in applied.line()
    added = {r.id for call in member.add_roles.await_args_list for r in call.args}
    removed = {r.id for call in member.remove_roles.await_args_list for r in call.args}
    assert any(guild.get_role(rid).name == "ANO" for rid in added)
    assert vets.id in removed, "the colour moves rather than doubling up"
    assert await guild_roles.resolve_role(guild, "ANO") is not None


async def test_apply_now_says_when_it_changed_nothing(db, monkeypatch):
    """The failure that took three bugs to spot: a command replying with
    confident success while doing nothing at all."""
    from hall_monitor.discord_bot.cogs.force import guild as force_guild
    from hall_monitor.services import athena_colour, major_guilds

    monkeypatch.setattr(
        "hall_monitor.services.guild_roles.settings.delegate_role_id", 100
    )
    athena_colour.reset_cache()

    async def unknown(guild_tag, *, urgent=False):
        return None

    async def major(tag):
        return True

    monkeypatch.setattr(athena_colour, "lookup", unknown)
    monkeypatch.setattr(major_guilds, "is_major", major)

    vets = _role(1, "VETS")
    delegate_role = _role(100, "Guild Hall Delegate")
    guild = _Guild([vets, delegate_role])
    await GuildRole.create(guild_tag="VETS", discord_role_id=1)
    await _delegate(1, tag="VETS", currently="VETS")
    # Already exactly where they should be.
    member = _member(guild, 1, holding=[vets, delegate_role])
    member.nick = "Tester [VETS]"

    ctx = MagicMock()
    ctx.guild = guild
    ctx.author = "janitor"
    applied = await force_guild.apply_now(ctx, member)

    assert applied.changed is False
    assert applied.line().startswith("Already standing `delegate`, wearing `VETS`")
    assert "nothing needed changing" in applied.line()


async def test_apply_now_reports_a_member_who_left(db):
    from hall_monitor.discord_bot.cogs.force import guild as force_guild

    guild = _Guild([])
    await _delegate(1, tag="VETS", currently="VETS")
    member = MagicMock()
    member.id = 1  # never added to the guild's member table

    ctx = MagicMock()
    ctx.guild = guild
    ctx.author = "janitor"
    applied = await force_guild.apply_now(ctx, member)

    assert "aren't in the server" in applied.line()
