"""The reconcile pass: contact roles gated on notability, guild roles
coloured or greyed to match, and spent guild roles recycled."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from hall_monitor.db.models import Delegate, GuildContact, GuildRole, NotabilityCache
from hall_monitor.services import athena_colour, guild_roles, notability, transitions

CONTACT_ROLE_IDS = {"events": 200, "housing": 201, "warring": 202, "ownership": 203}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for name, role_id in CONTACT_ROLE_IDS.items():
        monkeypatch.setattr(
            f"hall_monitor.services.contacts.settings.{name}_contact_role_id", role_id
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
        # The four contact roles exist in the server alongside whatever
        # guild roles a case sets up.
        self.roles = [
            _role(role_id, f"{name} contact")
            for name, role_id in CONTACT_ROLE_IDS.items()
        ] + list(roles)
        self.members = {}
        self.create_role = AsyncMock()

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    def get_member(self, user_id):
        return self.members.get(user_id)

    def add_member(self, user_id, *, holding=()):
        member = MagicMock()
        member.id = user_id
        member.guild = self
        member.roles = [_role(CONTACT_ROLE_IDS[name], name) for name in holding]
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        member.kick = AsyncMock()
        self.members[user_id] = member
        return member


async def _delegate(uuid, user_id, tag):
    return await Delegate.create(
        mc_uuid=uuid, discord_user_id=user_id, guild_tag=tag
    )


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
    member = guild.add_member(1, holding=("events",))
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    summary = await transitions.reconcile(guild)

    assert summary.guilds == 1 and summary.notable == 0
    assert summary.contacts_changed == 1
    assert summary.roles == {guild_roles.GREYED: 1}
    member.remove_roles.assert_awaited_once()
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
    assert member.add_roles.await_args.args[0].id == CONTACT_ROLE_IDS["events"]


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
