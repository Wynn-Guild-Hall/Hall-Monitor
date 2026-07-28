"""Contact slots: per-guild per-role uniqueness, displacement of the prior
holder, and the kick that follows losing your last contact role."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from hall_monitor.db.models import Delegate, GuildContact
from hall_monitor.services import contacts

CONTACT_ROLE_IDS = {"events": 200, "housing": 201, "warring": 202, "ownership": 203}


@pytest.fixture(autouse=True)
def configured_roles(monkeypatch):
    for name, role_id in CONTACT_ROLE_IDS.items():
        monkeypatch.setattr(
            f"hall_monitor.services.contacts.settings.{name}_contact_role_id", role_id
        )


def _fake_role(role_id: int, name: str):
    role = MagicMock()
    role.id = role_id
    role.name = name
    return role


class FakeGuild:
    """Just enough ``discord.Guild`` for the contact service: a role table
    and a member table it can look delegates up in."""

    def __init__(self):
        self.roles = {
            role_id: _fake_role(role_id, f"{name} contact")
            for name, role_id in CONTACT_ROLE_IDS.items()
        }
        self.members: dict[int, MagicMock] = {}

    def get_role(self, role_id):
        return self.roles.get(role_id)

    def get_member(self, user_id):
        return self.members.get(user_id)

    def add_member(self, user_id: int, *, holding: tuple[str, ...] = ()):
        member = MagicMock()
        member.id = user_id
        member.guild = self
        member.roles = [self.roles[CONTACT_ROLE_IDS[name]] for name in holding]
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        member.kick = AsyncMock()
        self.members[user_id] = member
        return member


async def _delegate(uuid: str, discord_user_id: int, tag: str = "VETS") -> Delegate:
    return await Delegate.create(
        mc_uuid=uuid, discord_user_id=discord_user_id, guild_tag=tag
    )


def _removed_role_ids(member) -> set[int]:
    return {
        role.id
        for call in member.remove_roles.await_args_list
        for role in call.args
    }


def _added_role_ids(member) -> set[int]:
    return {
        role.id for call in member.add_roles.await_args_list for role in call.args
    }


# --------------------------------------------------------------------------
# assign_contact
# --------------------------------------------------------------------------


async def test_assign_claims_a_free_slot(db):
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1)
    guild.add_member(1)

    assert (
        await contacts.assign_contact("VETS", "events", delegate, discord_guild=guild)
        is None
    )

    assert (await contacts.current_holder("VETS", "events")).id == delegate.id
    assert _added_role_ids(guild.members[1]) == {CONTACT_ROLE_IDS["events"]}


async def test_assign_displaces_prior_holder(db):
    """The prior holder loses the Discord role and the row moves — one
    contact per role per guild, enforced on both sides."""
    guild = FakeGuild()
    old = await _delegate("uuid-old", 1)
    new = await _delegate("uuid-new", 2)
    old_member = guild.add_member(1, holding=("events", "housing"))
    guild.add_member(2)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=old)
    await GuildContact.create(guild_tag="VETS", role="housing", delegate=old)

    displaced = await contacts.assign_contact(
        "VETS", "events", new, discord_guild=guild
    )

    assert displaced is not None
    assert displaced.delegate.id == old.id
    assert displaced.role == "events"
    assert displaced.kicked is False  # still the housing contact
    assert _removed_role_ids(old_member) == {CONTACT_ROLE_IDS["events"]}
    old_member.kick.assert_not_awaited()

    assert (await contacts.current_holder("VETS", "events")).id == new.id
    assert (await contacts.current_holder("VETS", "housing")).id == old.id
    # Displacement moves the row rather than stacking a second one.
    assert await GuildContact.filter(guild_tag="VETS", role="events").count() == 1


async def test_kick_when_last_contact_role_removed(db):
    guild = FakeGuild()
    old = await _delegate("uuid-old", 1)
    new = await _delegate("uuid-new", 2)
    old_member = guild.add_member(1, holding=("events",))
    guild.add_member(2)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=old)

    displaced = await contacts.assign_contact(
        "VETS", "events", new, discord_guild=guild
    )

    assert displaced.kicked is True
    old_member.kick.assert_awaited_once()
    # The kick is recorded, so a later re-verification isn't blocked by a
    # Delegate row that looks live.
    assert (await Delegate.get(id=old.id)).left_at is not None


async def test_kick_failure_is_reported_not_raised(db):
    """A 403 on the kick still leaves the slot with its new holder — the
    roster is what the DB says, and a janitor can remove them by hand."""
    guild = FakeGuild()
    old = await _delegate("uuid-old", 1)
    new = await _delegate("uuid-new", 2)
    old_member = guild.add_member(1, holding=("events",))
    guild.add_member(2)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=old)
    old_member.kick = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "role too high")
    )

    displaced = await contacts.assign_contact(
        "VETS", "events", new, discord_guild=guild
    )

    assert displaced.kicked is False
    assert (await contacts.current_holder("VETS", "events")).id == new.id


async def test_reassigning_the_same_delegate_displaces_nobody(db):
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1)
    member = guild.add_member(1, holding=("events",))
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    assert (
        await contacts.assign_contact("VETS", "events", delegate, discord_guild=guild)
        is None
    )

    member.kick.assert_not_awaited()
    member.remove_roles.assert_not_awaited()
    member.add_roles.assert_not_awaited()  # already holds it
    assert await GuildContact.filter(guild_tag="VETS", role="events").count() == 1


async def test_uniqueness_is_per_guild(db):
    """OTHR's events contact is untouched by VETS filling its own slot."""
    guild = FakeGuild()
    theirs = await _delegate("uuid-othr", 1, tag="OTHR")
    ours = await _delegate("uuid-vets", 2)
    their_member = guild.add_member(1, holding=("events",))
    guild.add_member(2)
    await GuildContact.create(guild_tag="OTHR", role="events", delegate=theirs)

    displaced = await contacts.assign_contact(
        "VETS", "events", ours, discord_guild=guild
    )

    assert displaced is None
    assert (await contacts.current_holder("OTHR", "events")).id == theirs.id
    their_member.remove_roles.assert_not_awaited()
    their_member.kick.assert_not_awaited()


async def test_guild_tag_matching_folds_case(db):
    """A janitor typing `vets` must hit the same slot the join flow wrote
    as `VETS`, or the displacement silently doesn't happen."""
    guild = FakeGuild()
    old = await _delegate("uuid-old", 1)
    new = await _delegate("uuid-new", 2)
    guild.add_member(1, holding=("events",))
    guild.add_member(2)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=old)

    displaced = await contacts.assign_contact(
        "vets", "events", new, discord_guild=guild
    )

    assert displaced is not None
    assert await GuildContact.filter(role="events").count() == 1
    assert (await contacts.current_holder("VETS", "events")).id == new.id


async def test_assign_refuses_a_delegate_playing_elsewhere(db):
    """Found in production: `~force assign` granted the role, reported
    success, and the next reconcile stripped it — because the two paths
    disagreed about whether an external rep may hold a contact role. The
    grant is the one that was wrong."""
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1)
    delegate.current_guild_tag = "OTHR"
    await delegate.save()
    member = guild.add_member(1)

    with pytest.raises(contacts.ExternalDelegate) as caught:
        await contacts.assign_contact("VETS", "events", delegate, discord_guild=guild)

    assert caught.value.playing_for == "OTHR"
    member.add_roles.assert_not_awaited()
    assert await contacts.current_holder("VETS", "events") is None, "no half-claim"


async def test_assign_allows_a_delegate_the_watch_hasnt_placed(db):
    """A NULL current guild means guildless or never polled, and neither
    is being external."""
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1)
    guild.add_member(1)

    await contacts.assign_contact("VETS", "events", delegate, discord_guild=guild)

    assert (await contacts.current_holder("VETS", "events")).id == delegate.id


async def test_assign_rejects_an_unknown_role(db):
    delegate = await _delegate("uuid-a", 1)
    with pytest.raises(contacts.UnknownContactRole):
        await contacts.assign_contact("VETS", "recruitment", delegate)


async def test_assign_without_a_discord_handle_still_moves_the_row(db):
    """The bot being disconnected must not lose the bookkeeping."""
    old = await _delegate("uuid-old", 1)
    new = await _delegate("uuid-new", 2)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=old)

    displaced = await contacts.assign_contact("VETS", "events", new)

    assert displaced.kicked is False  # nothing to kick them out of
    assert (await contacts.current_holder("VETS", "events")).id == new.id


async def test_grant_false_skips_the_redundant_role_add(db):
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1)
    member = guild.add_member(1)

    await contacts.assign_contact(
        "VETS", "events", delegate, discord_guild=guild, grant=False
    )

    member.add_roles.assert_not_awaited()
    assert (await contacts.current_holder("VETS", "events")).id == delegate.id


# --------------------------------------------------------------------------
# resolve_conflicts_and_kick_if_empty
# --------------------------------------------------------------------------


async def test_multi_role_claim_kicks_only_once_at_the_end(db):
    """Taking both of someone's slots kicks them on the second, not the
    first — the remaining count is re-read per role."""
    guild = FakeGuild()
    old = await _delegate("uuid-old", 1)
    new = await _delegate("uuid-new", 2)
    old_member = guild.add_member(1, holding=("events", "housing"))
    guild.add_member(2)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=old)
    await GuildContact.create(guild_tag="VETS", role="housing", delegate=old)

    displaced = await contacts.resolve_conflicts_and_kick_if_empty(
        "VETS", {"events", "housing"}, new, discord_guild=guild
    )

    assert {(d.role, d.kicked) for d in displaced} == {
        ("events", False),
        ("housing", True),
    }
    old_member.kick.assert_awaited_once()
    assert _removed_role_ids(old_member) == {
        CONTACT_ROLE_IDS["events"],
        CONTACT_ROLE_IDS["housing"],
    }


async def test_multi_role_claim_on_free_slots_displaces_nobody(db):
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1)
    guild.add_member(1)

    displaced = await contacts.resolve_conflicts_and_kick_if_empty(
        "VETS", {"events", "warring", "ownership"}, delegate, discord_guild=guild
    )

    assert displaced == []
    holders = await contacts.current_contacts_for_guild("VETS")
    assert {role for role, who in holders.items() if who} == {
        "events",
        "warring",
        "ownership",
    }


# --------------------------------------------------------------------------
# clear_contact (~unforce assign)
# --------------------------------------------------------------------------


async def test_clear_vacates_the_slot_and_kicks_when_last(db):
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1)
    member = guild.add_member(1, holding=("events",))
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    cleared = await contacts.clear_contact(
        "VETS", "events", delegate=delegate, discord_guild=guild
    )

    assert cleared is not None and cleared.kicked is True
    assert await contacts.current_holder("VETS", "events") is None
    assert _removed_role_ids(member) == {CONTACT_ROLE_IDS["events"]}
    member.kick.assert_awaited_once()


async def test_clear_keeps_a_holder_who_has_other_roles(db):
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1)
    member = guild.add_member(1, holding=("events", "warring"))
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)
    await GuildContact.create(guild_tag="VETS", role="warring", delegate=delegate)

    cleared = await contacts.clear_contact(
        "VETS", "events", delegate=delegate, discord_guild=guild
    )

    assert cleared.kicked is False
    member.kick.assert_not_awaited()
    assert (await contacts.current_holder("VETS", "warring")).id == delegate.id


async def test_clear_refuses_when_the_named_delegate_isnt_the_holder(db):
    """`~unforce assign @wrong-person events` must not evict the real one."""
    guild = FakeGuild()
    holder = await _delegate("uuid-holder", 1)
    other = await _delegate("uuid-other", 2)
    holder_member = guild.add_member(1, holding=("events",))
    await GuildContact.create(guild_tag="VETS", role="events", delegate=holder)

    assert (
        await contacts.clear_contact(
            "VETS", "events", delegate=other, discord_guild=guild
        )
        is None
    )
    assert (await contacts.current_holder("VETS", "events")).id == holder.id
    holder_member.remove_roles.assert_not_awaited()


async def test_clear_on_an_empty_slot_is_a_no_op(db):
    assert await contacts.clear_contact("VETS", "events") is None


# --------------------------------------------------------------------------
# sync_contact_roles — notability gating
# --------------------------------------------------------------------------


async def test_sync_withdraws_contact_roles_from_a_guild_that_lost_notability(db):
    """The contact channels are for guilds currently in the Hall."""
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1)
    member = guild.add_member(1, holding=("events", "warring"))
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)
    await GuildContact.create(guild_tag="VETS", role="warring", delegate=delegate)

    changed = await contacts.sync_contact_roles(
        "VETS", discord_guild=guild, granted=False
    )

    assert changed == 2
    assert _removed_role_ids(member) == {
        CONTACT_ROLE_IDS["events"],
        CONTACT_ROLE_IDS["warring"],
    }
    member.kick.assert_not_awaited()  # losing the guild's status isn't losing a slot


async def test_sync_keeps_the_slot_rows(db):
    """Clearing them would cost four re-verifications on the guild's return
    and lose the record of who to hand the roles back to."""
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1)
    guild.add_member(1, holding=("events",))
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    await contacts.sync_contact_roles("VETS", discord_guild=guild, granted=False)

    assert (await contacts.current_holder("VETS", "events")).id == delegate.id


async def test_sync_hands_the_roles_back_when_the_guild_returns(db):
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1)
    member = guild.add_member(1)  # holding nothing
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    changed = await contacts.sync_contact_roles(
        "VETS", discord_guild=guild, granted=True
    )

    assert changed == 1
    assert _added_role_ids(member) == {CONTACT_ROLE_IDS["events"]}


async def test_sync_is_silent_when_nothing_needs_changing(db):
    """It runs hourly — a pass with nothing to do must cost no requests."""
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1)
    member = guild.add_member(1, holding=("events",))
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    assert (
        await contacts.sync_contact_roles("VETS", discord_guild=guild, granted=True)
        == 0
    )
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()


async def test_sync_leaves_other_guilds_alone(db):
    guild = FakeGuild()
    theirs = await _delegate("uuid-othr", 1, tag="OTHR")
    their_member = guild.add_member(1, holding=("events",))
    await GuildContact.create(guild_tag="OTHR", role="events", delegate=theirs)

    assert (
        await contacts.sync_contact_roles("VETS", discord_guild=guild, granted=False)
        == 0
    )
    their_member.remove_roles.assert_not_awaited()


async def test_sync_without_a_discord_handle_does_nothing(db):
    delegate = await _delegate("uuid-a", 1)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)
    assert await contacts.sync_contact_roles("VETS", discord_guild=None, granted=False) == 0


# --------------------------------------------------------------------------
# Read side
# --------------------------------------------------------------------------


async def test_current_contacts_lists_unclaimed_slots_as_none(db):
    delegate = await _delegate("uuid-a", 1)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    holders = await contacts.current_contacts_for_guild("VETS")

    assert set(holders) == set(contacts.CONTACT_ROLES)
    assert holders["events"].id == delegate.id
    assert holders["housing"] is None


async def test_current_contacts_drops_a_holder_who_left_the_server(db):
    """The `/join` conflict warning shouldn't name someone who's gone."""
    guild = FakeGuild()  # no members registered
    delegate = await _delegate("uuid-a", 1)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    holders = await contacts.current_contacts_for_guild("VETS", discord_guild=guild)

    assert holders["events"] is None
