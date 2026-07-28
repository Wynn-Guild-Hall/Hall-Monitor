"""The reconcile pass: contact roles gated on notability, guild roles
coloured or greyed to match, and spent guild roles recycled."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from hall_monitor.db.models import Delegate, GuildContact, GuildRole, NotabilityCache
from hall_monitor.services import (
    athena_colour,
    delegate_registry,
    guild_roles,
    notability,
    transitions,
)

CONTACT_ROLE_IDS = {"events": 200, "housing": 201, "warring": 202, "ownership": 203}
STANDING_ROLE_IDS = {
    delegate_registry.DELEGATE: 100,
    delegate_registry.RELEGATE: 101,
    delegate_registry.EXTERNAL: 102,
}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for name, role_id in CONTACT_ROLE_IDS.items():
        monkeypatch.setattr(
            f"hall_monitor.services.contacts.settings.{name}_contact_role_id", role_id
        )
    for name, attribute in (
        (delegate_registry.DELEGATE, "delegate_role_id"),
        (delegate_registry.RELEGATE, "relegate_role_id"),
        (delegate_registry.EXTERNAL, "external_relegate_role_id"),
    ):
        monkeypatch.setattr(
            f"hall_monitor.services.guild_roles.settings.{attribute}",
            STANDING_ROLE_IDS[name],
        )
    athena_colour.reset_cache()

    async def unknown_to_athena(guild_tag, *, urgent=False):
        return None  # every guild gets the fallback colour; not what's under test

    monkeypatch.setattr(athena_colour, "lookup", unknown_to_athena)
    yield
    athena_colour.reset_cache()


@pytest.fixture
def notable(monkeypatch):
    """Control which tags count as notable without touching the APIs."""
    tags = set()

    async def fake_is_notable(tag):
        return tag.upper() in tags

    monkeypatch.setattr(notability, "is_notable", fake_is_notable)
    return tags


def _role(role_id, name, *, members=()):
    role = MagicMock()
    role.id = role_id
    role.name = name
    role.colour = MagicMock()
    role.mentionable = True
    role.members = list(members)
    role.edit = AsyncMock()
    role.delete = AsyncMock()
    return role


class FakeGuild:
    def __init__(self, roles=()):
        # The contact and standing roles exist in the server alongside
        # whatever guild roles a case sets up.
        self.roles = [
            _role(role_id, f"{name} contact")
            for name, role_id in CONTACT_ROLE_IDS.items()
        ] + [
            _role(role_id, name) for name, role_id in STANDING_ROLE_IDS.items()
        ] + list(roles)
        self.members = {}
        self.create_role = AsyncMock()

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    def get_member(self, user_id):
        return self.members.get(user_id)

    def add_member(self, user_id, *, holding=(), standing=None, guild_role=None):
        member = MagicMock()
        member.id = user_id
        member.guild = self
        member.roles = [_role(CONTACT_ROLE_IDS[name], name) for name in holding]
        if standing is not None:
            member.roles.append(_role(STANDING_ROLE_IDS[standing], standing))
        if guild_role is not None:
            member.roles.append(guild_role)
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        member.kick = AsyncMock()
        self.members[user_id] = member
        return member


async def _delegate(uuid, user_id, tag, *, currently=None):
    return await Delegate.create(
        mc_uuid=uuid,
        discord_user_id=user_id,
        guild_tag=tag,
        current_guild_tag=currently,
    )


def _added(member) -> set[int]:
    return {r.id for call in member.add_roles.await_args_list for r in call.args}


def _removed(member) -> set[int]:
    return {r.id for call in member.remove_roles.await_args_list for r in call.args}


# --------------------------------------------------------------------------
# Which guilds get visited
# --------------------------------------------------------------------------


async def test_only_guilds_with_a_presence_are_visited(db):
    """The cache knows a couple of hundred guilds. Reconciling them all
    would mint a role for every guild in Wynncraft."""
    await NotabilityCache.create(
        guild_tag="AAAA", is_notable=True, signals_json="{}"
    )
    await _delegate("uuid-a", 1, "VETS")
    await GuildRole.create(guild_tag="OTHR", discord_role_id=9)

    assert await transitions.guilds_present() == ["OTHR", "VETS"]


async def test_a_guild_is_visited_once_whatever_its_spelling(db):
    """`VETS` and `vets` are one guild; visiting both would have the
    second pass undo the first."""
    await _delegate("uuid-a", 1, "VETS")
    delegate = await _delegate("uuid-b", 2, "vets")
    await GuildContact.create(guild_tag="vets", role="events", delegate=delegate)

    assert len(await transitions.guilds_present()) == 1


async def test_a_delegate_who_left_doesnt_keep_a_guild_alive(db):
    delegate = await _delegate("uuid-a", 1, "VETS")
    delegate.left_at = delegate.joined_at
    await delegate.save()

    assert await transitions.guilds_present() == []


# --------------------------------------------------------------------------
# What the pass settles
# --------------------------------------------------------------------------


async def test_a_non_notable_guild_loses_its_contact_roles_and_colour(db, notable):
    role = _role(1, "VETS", members=[MagicMock()])
    guild = FakeGuild([role])
    await GuildRole.create(guild_tag="VETS", discord_role_id=1)
    delegate = await _delegate("uuid-a", 1, "VETS")
    member = guild.add_member(
        1, holding=("events",), standing=delegate_registry.DELEGATE, guild_role=role
    )
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    summary = await transitions.reconcile(guild)

    assert summary.guilds == 1 and summary.notable == 0
    assert summary.contacts_changed == 1
    assert summary.roles == {guild_roles.GREYED: 1}
    assert CONTACT_ROLE_IDS["events"] in _removed(member)
    member.kick.assert_not_awaited()


async def test_a_returning_guild_gets_its_contacts_back(db, notable):
    notable.add("VETS")
    role = _role(1, "VETS", members=[MagicMock()])
    guild = FakeGuild([role])
    await GuildRole.create(guild_tag="VETS", discord_role_id=1)
    delegate = await _delegate("uuid-a", 1, "VETS")
    member = guild.add_member(1)  # roles were withdrawn while it was quiet
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    summary = await transitions.reconcile(guild)

    assert summary.notable == 1
    assert summary.contacts_changed == 1
    assert CONTACT_ROLE_IDS["events"] in _added(member)


# --------------------------------------------------------------------------
# Standing: delegate ↔ relegate ↔ external relegate
# --------------------------------------------------------------------------


async def test_losing_notability_relegates_the_representatives(db, notable):
    role = _role(1, "VETS", members=[MagicMock()])
    guild = FakeGuild([role])
    await _delegate("uuid-a", 1, "VETS")
    member = guild.add_member(1, standing=delegate_registry.DELEGATE, guild_role=role)

    await transitions.reconcile(guild)

    assert STANDING_ROLE_IDS[delegate_registry.RELEGATE] in _added(member)
    assert STANDING_ROLE_IDS[delegate_registry.DELEGATE] in _removed(member)
    # They keep the guild's role: they still represent it, quiet quarter or not.
    assert role.id not in _removed(member)


async def test_regaining_notability_promotes_them_back(db, notable):
    notable.add("VETS")
    role = _role(1, "VETS", members=[MagicMock()])
    guild = FakeGuild([role])
    await _delegate("uuid-a", 1, "VETS")
    member = guild.add_member(1, standing=delegate_registry.RELEGATE, guild_role=role)

    await transitions.reconcile(guild)

    assert STANDING_ROLE_IDS[delegate_registry.DELEGATE] in _added(member)
    assert STANDING_ROLE_IDS[delegate_registry.RELEGATE] in _removed(member)


async def test_a_rep_who_moved_guilds_becomes_an_external_relegate(db, notable):
    """Wearing VETS while playing for OTHR misinforms the whole room."""
    notable.add("VETS")
    role = _role(1, "VETS", members=[MagicMock()])
    guild = FakeGuild([role])
    await _delegate("uuid-a", 1, "VETS", currently="OTHR")
    member = guild.add_member(1, standing=delegate_registry.DELEGATE, guild_role=role)

    await transitions.reconcile(guild)

    assert STANDING_ROLE_IDS[delegate_registry.EXTERNAL] in _added(member)
    assert STANDING_ROLE_IDS[delegate_registry.DELEGATE] in _removed(member)
    assert role.id in _removed(member), "out of the guild's own role"


async def test_moving_guilds_outranks_the_guild_being_notable(db, notable):
    notable.add("VETS")
    delegate = await _delegate("uuid-a", 1, "VETS", currently="OTHR")
    assert (
        delegate_registry.standing(delegate, notable=True)
        == delegate_registry.EXTERNAL
    )


async def test_a_guildless_rep_is_left_alone(db, notable):
    """Between guilds is not the same as gone — relegating someone who
    left for an afternoon is the behaviour the brief rules out."""
    notable.add("VETS")
    role = _role(1, "VETS", members=[MagicMock()])
    guild = FakeGuild([role])
    await _delegate("uuid-a", 1, "VETS", currently=None)
    member = guild.add_member(1, standing=delegate_registry.DELEGATE, guild_role=role)

    await transitions.reconcile(guild)

    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()


async def test_an_external_rep_loses_their_contact_roles(db, notable):
    notable.add("VETS")
    role = _role(1, "VETS", members=[MagicMock()])
    guild = FakeGuild([role])
    delegate = await _delegate("uuid-a", 1, "VETS", currently="OTHR")
    member = guild.add_member(1, holding=("events",), guild_role=role)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    await transitions.reconcile(guild)

    assert CONTACT_ROLE_IDS["events"] in _removed(member)
    # The slot is still theirs on paper — nothing re-verifies to get it back.
    assert await GuildContact.filter(guild_tag="VETS", role="events").count() == 1


async def test_a_settled_member_costs_no_requests(db, notable):
    """It runs hourly; a member already in the right state must be quiet."""
    notable.add("VETS")
    role = _role(1, "VETS", members=[MagicMock()])
    guild = FakeGuild([role])
    await _delegate("uuid-a", 1, "VETS")
    member = guild.add_member(1, standing=delegate_registry.DELEGATE, guild_role=role)

    summary = await transitions.reconcile(guild)

    assert summary.members_changed == 0
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()


async def test_a_rep_who_left_the_server_is_skipped(db, notable):
    """The leave path owns that; this pass can't act on an absent member."""
    guild = FakeGuild([_role(1, "VETS")])
    await _delegate("uuid-a", 1, "VETS")  # no matching Discord member

    summary = await transitions.reconcile(guild)

    assert summary.members_changed == 0


async def test_a_spent_role_is_recycled(db, notable):
    """Nobody wearing it, no delegates left — it costs a role slot and
    holds nothing. The next join recreates it."""
    role = _role(1, "VETS")
    guild = FakeGuild([role])
    await GuildRole.create(guild_tag="VETS", discord_role_id=1)

    summary = await transitions.reconcile(guild)

    assert summary.roles == {guild_roles.DELETED: 1}
    role.delete.assert_awaited_once()
    assert await GuildRole.all().count() == 0


# --------------------------------------------------------------------------
# The guild watch — Wynncraft pushes nothing, so it's a poll
# --------------------------------------------------------------------------


def _in_guild(prefix):
    guild = MagicMock()
    guild.prefix = prefix
    return guild


async def test_the_watch_records_a_move(db, monkeypatch):
    await _delegate("uuid-a", 1, "VETS")

    async def moved(uuid, *, urgent=False):
        return _in_guild("OTHR")

    monkeypatch.setattr(
        "hall_monitor.services.delegate_registry.wynncraft.get_player_guild", moved
    )

    checked, external = await delegate_registry.refresh_current_guilds()

    assert (checked, external) == (1, 1)
    assert (await Delegate.get(mc_uuid="uuid-a")).current_guild_tag == "OTHR"


async def test_the_watch_records_going_guildless_as_no_move(db, monkeypatch):
    await _delegate("uuid-a", 1, "VETS", currently="OTHR")

    async def guildless(uuid, *, urgent=False):
        return None

    monkeypatch.setattr(
        "hall_monitor.services.delegate_registry.wynncraft.get_player_guild", guildless
    )

    checked, external = await delegate_registry.refresh_current_guilds()

    assert (checked, external) == (1, 0)
    assert (await Delegate.get(mc_uuid="uuid-a")).current_guild_tag is None


async def test_a_failed_lookup_keeps_the_stored_guild(db, monkeypatch):
    """A 429 recorded as "no guild" would read back as them having
    rejoined, quietly promoting someone the last sweep relegated."""
    await _delegate("uuid-a", 1, "VETS", currently="OTHR")

    async def rate_limited(uuid, *, urgent=False):
        raise httpx.HTTPStatusError("429", request=MagicMock(), response=MagicMock())

    monkeypatch.setattr(
        "hall_monitor.services.delegate_registry.wynncraft.get_player_guild",
        rate_limited,
    )

    checked, _ = await delegate_registry.refresh_current_guilds()

    assert checked == 0
    assert (await Delegate.get(mc_uuid="uuid-a")).current_guild_tag == "OTHR"


async def test_the_watch_ignores_delegates_who_left_the_server(db, monkeypatch):
    delegate = await _delegate("uuid-a", 1, "VETS")
    delegate.left_at = delegate.joined_at
    await delegate.save()
    called = False

    async def counting(uuid, *, urgent=False):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(
        "hall_monitor.services.delegate_registry.wynncraft.get_player_guild", counting
    )

    await delegate_registry.refresh_current_guilds()

    assert not called, "no point spending a request on someone who's gone"


async def test_returning_to_their_own_guild_clears_external_standing(db, monkeypatch):
    """Self-healing: the standing follows the poll, not a remembered event."""
    await _delegate("uuid-a", 1, "VETS", currently="OTHR")

    async def back_home(uuid, *, urgent=False):
        return _in_guild("VETS")

    monkeypatch.setattr(
        "hall_monitor.services.delegate_registry.wynncraft.get_player_guild", back_home
    )

    _, external = await delegate_registry.refresh_current_guilds()

    assert external == 0
    assert not delegate_registry.is_external(await Delegate.get(mc_uuid="uuid-a"))


async def test_the_watch_folds_case_on_the_tag(db, monkeypatch):
    """Wynncraft's spelling isn't guaranteed to match what we stored."""
    await _delegate("uuid-a", 1, "VETS")

    async def same_guild_other_case(uuid, *, urgent=False):
        return _in_guild("vets")

    monkeypatch.setattr(
        "hall_monitor.services.delegate_registry.wynncraft.get_player_guild",
        same_guild_other_case,
    )

    _, external = await delegate_registry.refresh_current_guilds()

    assert external == 0


async def test_one_guilds_failure_doesnt_stop_the_pass(db, notable, monkeypatch):
    notable.update({"VETS", "OTHR"})
    ours = _role(1, "VETS", members=[MagicMock()])
    theirs = _role(2, "OTHR", members=[MagicMock()])
    guild = FakeGuild([ours, theirs])
    await GuildRole.create(guild_tag="VETS", discord_role_id=1)
    await GuildRole.create(guild_tag="OTHR", discord_role_id=2)

    real = guild_roles.reconcile_role

    async def explode_on_vets(discord_guild, tag, *, notable):
        if tag == "VETS":
            raise RuntimeError("Discord had an opinion")
        return await real(discord_guild, tag, notable=notable)

    monkeypatch.setattr(guild_roles, "reconcile_role", explode_on_vets)

    summary = await transitions.reconcile(guild)

    assert summary.failed == 1
    assert summary.roles, "OTHR was still settled"


async def test_the_summary_reads_as_a_log_line(db, notable):
    summary = await transitions.reconcile(FakeGuild())
    assert "0 guilds (0 notable)" in summary.line()
    assert "nothing to do" in summary.line()
