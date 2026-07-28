"""PendingInvite invariants — one-per-uuid, sweep revokes stale, re-request
revokes old, reject-if-already-delegate — plus the join side: matching a new
member to the invite they used and applying the roles it carried."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from hall_monitor.db.models import Delegate, GuildContact, PendingInvite
from hall_monitor.discord_bot.cogs.listeners import on_join
from hall_monitor.services import contacts, discord_invites
from hall_monitor.services.discord_invites import (
    AlreadyLiveDelegate,
    INVITE_MAX_AGE_SECONDS,
    mint_invite,
    resolve_used_invite,
    revoke_invite,
    sweep_expired,
)

GUILD_ID = 7
DELEGATE_ROLE_ID = 100
CONTACT_ROLE_IDS = {"events": 200, "housing": 201, "warring": 202, "ownership": 203}
GUILD_TAG_ROLE_ID = 300


@pytest.fixture(autouse=True)
def clean_snapshot():
    """The invite snapshot is module state; don't let it leak between tests."""
    discord_invites.reset_snapshot()
    yield
    discord_invites.reset_snapshot()


@pytest.fixture(autouse=True)
def offline_athena(monkeypatch):
    """The aesthetic role's colour is Stage 8's subject, not this file's —
    but the join path asks for it, and nothing here should reach Athena."""

    async def unknown_to_athena(guild_tag, *, urgent=False):
        return None

    monkeypatch.setattr(
        "hall_monitor.services.athena_colour.lookup", unknown_to_athena
    )


@pytest.fixture
def configured_roles(monkeypatch):
    """Point the settings singleton at the fake guild's role IDs."""
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.listeners.on_join.settings.discord_guild_id",
        GUILD_ID,
    )
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.listeners.on_join.settings.delegate_role_id",
        DELEGATE_ROLE_ID,
    )
    for name, role_id in CONTACT_ROLE_IDS.items():
        monkeypatch.setattr(
            f"hall_monitor.services.contacts.settings.{name}_contact_role_id",
            role_id,
        )


def _fake_invite(code: str):
    invite = MagicMock()
    invite.code = code
    invite.url = f"https://discord.gg/{code}"
    return invite


def _listed_invite(code: str, uses: int = 0):
    """An invite as it comes back from ``Guild.invites()`` — ``uses`` is a
    real int here because the diff does arithmetic on it."""
    invite = MagicMock()
    invite.code = code
    invite.uses = uses
    return invite


def _fake_role(role_id: int, name: str):
    role = MagicMock()
    role.id = role_id
    role.name = name
    return role


def _all_roles() -> dict[int, MagicMock]:
    roles = {DELEGATE_ROLE_ID: _fake_role(DELEGATE_ROLE_ID, "Guild Hall Delegate")}
    for name, role_id in CONTACT_ROLE_IDS.items():
        roles[role_id] = _fake_role(role_id, f"{name.title()} Contact")
    return roles


def _fake_guild(
    *listed,
    roles: dict | None = None,
    guild_id: int = GUILD_ID,
    vanity: str | None = None,
):
    guild = MagicMock()
    guild.id = guild_id
    guild.vanity_url_code = vanity
    guild.invites = AsyncMock(return_value=list(listed))
    table = _all_roles() if roles is None else roles
    guild.get_role = table.get
    # The guild-tag role doesn't exist yet, so the join mints it.
    guild.roles = list(table.values())
    guild.create_role = AsyncMock(
        side_effect=lambda *, name, **kwargs: _fake_role(GUILD_TAG_ROLE_ID, name)
    )
    return guild


def _fake_member(guild, user_id: int = 555, *, bot: bool = False):
    member = MagicMock()
    member.id = user_id
    member.bot = bot
    member.guild = guild
    member.name = "Tester"
    member.nick = None  # fresh join: the nickname is ours to pick
    member.roles = []
    member.add_roles = AsyncMock()
    member.edit = AsyncMock()
    return member


def _forbidden():
    return discord.Forbidden(MagicMock(status=403), "missing permissions")


async def _pending(code: str, *, mc_uuid: str = "uuid-1", bits: int = 5, tag: str = "VETS"):
    """A PendingInvite row whose code is also being tracked in the snapshot,
    i.e. the state ``mint_invite`` would have left behind."""
    discord_invites.note_minted(code)
    return await PendingInvite.create(
        mc_uuid=mc_uuid, guild_tag=tag, roles_bits=bits, discord_invite_code=code
    )


def _fake_channel(mint_code: str = "code1"):
    channel = MagicMock()
    channel.create_invite = AsyncMock(return_value=_fake_invite(mint_code))
    return channel


def _fake_bot():
    bot = MagicMock()
    bot.http = MagicMock()
    bot.http.delete_invite = AsyncMock()
    return bot


def _guild_with_member(discord_user_id: int | None):
    """A discord.Guild-like mock whose get_member returns a live member for
    ``discord_user_id`` (or None if that id doesn't match)."""
    guild = MagicMock()
    member = MagicMock() if discord_user_id is not None else None

    def get_member(uid):
        return member if uid == discord_user_id else None

    guild.get_member = get_member
    return guild


async def test_mint_persists_pending_invite_row(db):
    channel = _fake_channel("abc123")
    row = await mint_invite("uuid-1", "VETS", 5, channel=channel)
    assert row.mc_uuid == "uuid-1"
    assert row.guild_tag == "VETS"
    assert row.roles_bits == 5
    assert row.discord_invite_code == "abc123"
    channel.create_invite.assert_awaited_once()
    kwargs = channel.create_invite.await_args.kwargs
    assert kwargs["max_uses"] == 1
    assert kwargs["max_age"] == INVITE_MAX_AGE_SECONDS


async def test_mint_enforces_one_row_per_uuid_and_revokes_prior(db):
    """Re-requesting for the same UUID revokes the old Discord invite and
    replaces the DB row with a fresh one."""
    channel = _fake_channel("first")
    bot = _fake_bot()
    await mint_invite("uuid-1", "VETS", 5, channel=channel, bot=bot)

    channel.create_invite = AsyncMock(return_value=_fake_invite("second"))
    row = await mint_invite("uuid-1", "VETS", 3, channel=channel, bot=bot)

    # DB has exactly one row for this UUID, and it's the fresh one.
    rows = await PendingInvite.filter(mc_uuid="uuid-1").all()
    assert len(rows) == 1
    assert rows[0].discord_invite_code == "second"
    assert row.roles_bits == 3

    # Prior code was revoked.
    bot.http.delete_invite.assert_awaited_once()
    args, _ = bot.http.delete_invite.await_args
    assert args[0] == "first"


async def test_mint_rejects_when_uuid_already_a_live_delegate(db):
    await Delegate.create(
        mc_uuid="uuid-1", discord_user_id=42, guild_tag="VETS"
    )
    guild = _guild_with_member(42)
    channel = _fake_channel("abc")
    with pytest.raises(AlreadyLiveDelegate):
        await mint_invite(
            "uuid-1", "VETS", 5, channel=channel, discord_guild=guild
        )
    channel.create_invite.assert_not_awaited()


async def test_mint_ignores_delegate_row_whose_member_left(db):
    """A Delegate row whose Discord user has vanished from the server must
    not block re-verification — is_current_member returns False."""
    await Delegate.create(
        mc_uuid="uuid-1", discord_user_id=42, guild_tag="VETS"
    )
    guild_without_member = _guild_with_member(None)
    channel = _fake_channel("fresh")
    row = await mint_invite(
        "uuid-1", "VETS", 1, channel=channel, discord_guild=guild_without_member
    )
    assert row.discord_invite_code == "fresh"


async def test_revoke_swallows_not_found(db):
    bot = _fake_bot()
    bot.http.delete_invite = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "gone")
    )
    # Should not raise.
    await revoke_invite("missing-code", bot=bot)


async def test_sweep_deletes_rows_older_than_ttl(db, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.services.discord_invites.settings.pending_invite_ttl_minutes",
        45,
    )
    fresh = await PendingInvite.create(
        mc_uuid="fresh", guild_tag="VETS", roles_bits=1, discord_invite_code="fresh-c"
    )
    stale = await PendingInvite.create(
        mc_uuid="stale", guild_tag="VETS", roles_bits=1, discord_invite_code="stale-c"
    )
    # Backdate the stale row by more than the TTL.
    stale.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await stale.save()

    bot = _fake_bot()
    deleted = await sweep_expired(bot=bot)
    assert deleted == 1
    remaining = {r.mc_uuid for r in await PendingInvite.all()}
    assert remaining == {"fresh"}
    bot.http.delete_invite.assert_awaited_once()
    args, _ = bot.http.delete_invite.await_args
    assert args[0] == "stale-c"


async def test_sweep_without_bot_still_deletes_rows(db, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.services.discord_invites.settings.pending_invite_ttl_minutes",
        45,
    )
    stale = await PendingInvite.create(
        mc_uuid="stale", guild_tag="VETS", roles_bits=1, discord_invite_code="stale-c"
    )
    stale.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await stale.save()
    assert await sweep_expired() == 1
    assert await PendingInvite.filter(mc_uuid="stale").count() == 0


async def test_sweep_no_op_when_nothing_stale(db):
    await PendingInvite.create(
        mc_uuid="u", guild_tag="VETS", roles_bits=0, discord_invite_code="c"
    )
    assert await sweep_expired() == 0
    assert await PendingInvite.filter().count() == 1


# --------------------------------------------------------------------------
# Invite-usage snapshot
# --------------------------------------------------------------------------


async def test_mint_tracks_the_new_code_in_the_snapshot(db):
    await mint_invite("uuid-1", "VETS", 5, channel=_fake_channel("abc123"))
    assert discord_invites.snapshot() == {"abc123": 0}


async def test_revoke_forgets_the_code(db):
    discord_invites.note_minted("gone")
    await revoke_invite("gone", bot=_fake_bot())
    assert discord_invites.snapshot() == {}


async def test_revoke_keeps_tracking_when_discord_errors(db):
    """A 500 means the invite may well still be live — keep watching it."""
    bot = _fake_bot()
    bot.http.delete_invite = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "boom")
    )
    discord_invites.note_minted("maybe-live")
    with pytest.raises(discord.HTTPException):
        await revoke_invite("maybe-live", bot=bot)
    assert discord_invites.snapshot() == {"maybe-live": 0}


async def test_refresh_snapshot_replaces_contents(db):
    discord_invites.note_minted("stale")
    guild = _fake_guild(_listed_invite("a", 3), _listed_invite("b", 0))
    assert await discord_invites.refresh_snapshot(guild) == {"a": 3, "b": 0}
    assert discord_invites.snapshot() == {"a": 3, "b": 0}


async def test_snapshot_excludes_the_vanity_url(db):
    """The vanity code's use count climbs forever; tracking it would flag a
    use on every join."""
    guild = _fake_guild(
        _listed_invite("wynnvets", 812), _listed_invite("a", 0), vanity="wynnvets"
    )
    assert await discord_invites.refresh_snapshot(guild) == {"a": 0}


# --------------------------------------------------------------------------
# resolve_used_invite
# --------------------------------------------------------------------------


async def test_resolve_matches_the_code_that_vanished(db):
    """The real shape for us: a max_uses=1 invite is deleted on consumption,
    so the used code is simply absent from the fresh list."""
    row = await _pending("used-code")
    member = _fake_member(_fake_guild())
    resolved = await resolve_used_invite(member)
    assert resolved is not None
    assert resolved.id == row.id


async def test_resolve_matches_incremented_uses(db):
    row = await _pending("still-listed")
    member = _fake_member(_fake_guild(_listed_invite("still-listed", 1)))
    resolved = await resolve_used_invite(member)
    assert resolved is not None
    assert resolved.id == row.id


async def test_resolve_prefers_the_used_code_over_a_simultaneous_expiry(db):
    """An expired invite and a consumed one both leave the list. The one
    Discord still reports with a bumped use count wins."""
    await _pending("expired", mc_uuid="uuid-expired")
    used = await _pending("used", mc_uuid="uuid-used")
    member = _fake_member(_fake_guild(_listed_invite("used", 1)))
    resolved = await resolve_used_invite(member)
    assert resolved is not None
    assert resolved.mc_uuid == used.mc_uuid


async def test_resolve_ignores_invites_we_dont_own(db):
    """Someone else's invite expiring must not be read as one of ours."""
    discord_invites.note_minted("not-ours")
    member = _fake_member(_fake_guild())
    assert await resolve_used_invite(member) is None


async def test_resolve_bails_when_two_of_ours_change_at_once(db):
    await _pending("code-a", mc_uuid="uuid-a")
    await _pending("code-b", mc_uuid="uuid-b")
    member = _fake_member(_fake_guild())
    assert await resolve_used_invite(member) is None
    # Both rows survive for the sweep rather than one being guessed at.
    assert await PendingInvite.all().count() == 2


async def test_resolve_skips_rows_past_the_invite_max_age(db):
    """A quiet period's worth of expiries shouldn't make a real join
    ambiguous — an invite older than its own max_age can't be the one
    just consumed."""
    old = await _pending("long-expired", mc_uuid="uuid-old")
    old.created_at = datetime.now(timezone.utc) - timedelta(
        seconds=INVITE_MAX_AGE_SECONDS * 3
    )
    await old.save()
    fresh = await _pending("just-used", mc_uuid="uuid-fresh")

    member = _fake_member(_fake_guild())
    resolved = await resolve_used_invite(member)
    assert resolved is not None
    assert resolved.id == fresh.id


async def test_resolve_returns_none_without_manage_server(db):
    await _pending("used-code")
    guild = _fake_guild()
    guild.invites = AsyncMock(side_effect=_forbidden())
    assert await resolve_used_invite(_fake_member(guild)) is None
    # Snapshot untouched, so a later join can still resolve once the
    # permission is granted.
    assert discord_invites.snapshot() == {"used-code": 0}


async def test_resolve_updates_the_snapshot_to_the_fresh_list(db):
    await _pending("used-code")
    member = _fake_member(_fake_guild(_listed_invite("someone-elses", 4)))
    await resolve_used_invite(member)
    assert discord_invites.snapshot() == {"someone-elses": 4}


# --------------------------------------------------------------------------
# resolve_roles
# --------------------------------------------------------------------------


def test_resolve_roles_maps_bits_to_configured_roles(configured_roles):
    guild = _fake_guild()
    roles = on_join.resolve_roles(guild, 0b0101)  # events + warring
    assert {role.id for role in roles} == {
        DELEGATE_ROLE_ID,
        CONTACT_ROLE_IDS["events"],
        CONTACT_ROLE_IDS["warring"],
    }


def test_resolve_roles_rejects_an_unconfigured_role(configured_roles, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.services.contacts.settings.housing_contact_role_id", 0
    )
    with pytest.raises(on_join.RoleResolutionError):
        on_join.resolve_roles(_fake_guild(), 0b0010)  # housing


def test_resolve_roles_rejects_a_role_missing_from_the_guild(configured_roles):
    roles = _all_roles()
    del roles[CONTACT_ROLE_IDS["ownership"]]
    with pytest.raises(on_join.RoleResolutionError):
        on_join.resolve_roles(_fake_guild(roles=roles), 0b1000)  # ownership


# --------------------------------------------------------------------------
# on_member_join
# --------------------------------------------------------------------------


async def test_join_applies_roles_and_promotes_to_delegate(db, configured_roles):
    await _pending("used-code", mc_uuid="uuid-chief", bits=0b0101, tag="VETS")
    guild = _fake_guild()
    member = _fake_member(guild, user_id=555)

    await on_join.OnJoin(MagicMock()).on_member_join(member)

    member.add_roles.assert_awaited_once()
    applied = {role.id for role in member.add_roles.await_args.args}
    assert applied == {
        DELEGATE_ROLE_ID,
        CONTACT_ROLE_IDS["events"],
        CONTACT_ROLE_IDS["warring"],
        GUILD_TAG_ROLE_ID,  # the guild's aesthetic role, minted on demand
    }
    assert "VETS" in member.add_roles.await_args.kwargs["reason"]

    delegate = await Delegate.get(mc_uuid="uuid-chief")
    assert delegate.discord_user_id == 555
    assert delegate.guild_tag == "VETS"
    assert await PendingInvite.filter(discord_invite_code="used-code").count() == 0


async def test_join_claims_the_contact_slots_and_displaces_the_prior_holder(
    db, configured_roles
):
    """Verification is how a guild swaps a contact — the join has to run the
    same displacement path `~force assign` does."""
    await _pending("used-code", mc_uuid="uuid-new", bits=0b0001, tag="VETS")
    old = await Delegate.create(
        mc_uuid="uuid-old", discord_user_id=999, guild_tag="VETS"
    )
    await GuildContact.create(guild_tag="VETS", role="events", delegate=old)

    old_member = MagicMock()
    old_member.id = 999
    old_member.remove_roles = AsyncMock()
    old_member.kick = AsyncMock()
    guild = _fake_guild()
    old_member.guild = guild
    guild.get_member = lambda uid: old_member if uid == 999 else None
    member = _fake_member(guild, user_id=555)

    await on_join.OnJoin(MagicMock()).on_member_join(member)

    holder = await contacts.current_holder("VETS", "events")
    assert holder.discord_user_id == 555
    old_member.remove_roles.assert_awaited_once()
    old_member.kick.assert_awaited_once()  # events was their only slot


async def test_join_doesnt_re_add_roles_it_already_applied(db, configured_roles):
    """`add_roles` covers the whole set in one request; the slot claim must
    not spend four more calls re-applying them."""
    await _pending("used-code", mc_uuid="uuid-new", bits=0b1111, tag="VETS")
    guild = _fake_guild()
    member = _fake_member(guild, user_id=555)
    guild.get_member = lambda uid: member if uid == 555 else None

    await on_join.OnJoin(MagicMock()).on_member_join(member)

    member.add_roles.assert_awaited_once()
    assert await GuildContact.filter(guild_tag="VETS").count() == 4


async def test_join_mints_the_guild_role_in_the_same_request(db, configured_roles):
    """The aesthetic role is created on demand and applied with the rest —
    a second `add_roles` would be rate limit spent on a colour."""
    await _pending("used-code", mc_uuid="uuid-chief", bits=0b0001, tag="VETS")
    guild = _fake_guild()
    member = _fake_member(guild, user_id=555)

    await on_join.OnJoin(MagicMock()).on_member_join(member)

    guild.create_role.assert_awaited_once()
    assert guild.create_role.await_args.kwargs["name"] == "VETS"
    member.add_roles.assert_awaited_once()
    assert GUILD_TAG_ROLE_ID in {role.id for role in member.add_roles.await_args.args}


async def test_join_names_them_after_their_minecraft_account(db, configured_roles):
    """A join is the one time the visible part is ours to pick."""
    await _pending("used-code", mc_uuid="uuid-chief", bits=0b0001, tag="VETS")
    pending = await PendingInvite.get(discord_invite_code="used-code")
    pending.mc_username = "Holidaze"
    await pending.save()
    member = _fake_member(_fake_guild(), user_id=555)

    await on_join.OnJoin(MagicMock()).on_member_join(member)

    assert member.edit.await_args.kwargs["nick"] == "Holidaze [VETS]"


async def test_join_survives_a_guild_role_it_cant_create(db, configured_roles):
    """Missing Manage Roles for a *new* role can't cost someone their
    verification — the delegate and contact roles are the point."""
    guild = _fake_guild()
    guild.create_role = AsyncMock(side_effect=_forbidden())
    await _pending("used-code", mc_uuid="uuid-chief", bits=0b0001, tag="VETS")
    member = _fake_member(guild, user_id=555)

    await on_join.OnJoin(MagicMock()).on_member_join(member)

    applied = {role.id for role in member.add_roles.await_args.args}
    assert applied == {DELEGATE_ROLE_ID, CONTACT_ROLE_IDS["events"]}
    assert (await Delegate.get(mc_uuid="uuid-chief")).discord_user_id == 555


async def test_join_without_a_match_changes_nothing(db, configured_roles):
    member = _fake_member(_fake_guild())
    await on_join.OnJoin(MagicMock()).on_member_join(member)
    member.add_roles.assert_not_awaited()
    assert await Delegate.all().count() == 0


async def test_join_keeps_the_pending_invite_when_role_apply_fails(db, configured_roles):
    """Bot lacks Manage Roles (or the role sits above it): no half-verified
    delegate, and the row stays for the sweep so a retry can re-mint."""
    await _pending("used-code", mc_uuid="uuid-chief")
    member = _fake_member(_fake_guild())
    member.add_roles = AsyncMock(side_effect=_forbidden())

    await on_join.OnJoin(MagicMock()).on_member_join(member)

    assert await Delegate.all().count() == 0
    assert await PendingInvite.filter(discord_invite_code="used-code").count() == 1


async def test_join_keeps_the_pending_invite_when_a_role_is_unconfigured(
    db, configured_roles, monkeypatch
):
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.listeners.on_join.settings.delegate_role_id", 0
    )
    await _pending("used-code", mc_uuid="uuid-chief")
    member = _fake_member(_fake_guild())

    await on_join.OnJoin(MagicMock()).on_member_join(member)

    member.add_roles.assert_not_awaited()
    assert await Delegate.all().count() == 0
    assert await PendingInvite.filter(discord_invite_code="used-code").count() == 1


async def test_join_keeps_the_pending_invite_on_an_unknown_role_bit(db, configured_roles):
    """A row written before a ROLE_BITS entry was retired still shouldn't
    take the member down a partial-role path."""
    await _pending("used-code", mc_uuid="uuid-chief", bits=1 << 6)
    member = _fake_member(_fake_guild())

    await on_join.OnJoin(MagicMock()).on_member_join(member)

    member.add_roles.assert_not_awaited()
    assert await PendingInvite.filter(discord_invite_code="used-code").count() == 1


async def test_join_ignores_bots(db, configured_roles):
    """A bot added via OAuth consumes no invite — don't let its join eat a
    representative's pending row."""
    await _pending("used-code", mc_uuid="uuid-chief")
    guild = _fake_guild()
    member = _fake_member(guild, bot=True)

    await on_join.OnJoin(MagicMock()).on_member_join(member)

    guild.invites.assert_not_awaited()
    assert await PendingInvite.filter(discord_invite_code="used-code").count() == 1


async def test_join_ignores_other_guilds(db, configured_roles):
    await _pending("used-code", mc_uuid="uuid-chief")
    guild = _fake_guild(guild_id=GUILD_ID + 1)
    member = _fake_member(guild)

    await on_join.OnJoin(MagicMock()).on_member_join(member)

    guild.invites.assert_not_awaited()
    member.add_roles.assert_not_awaited()


async def test_on_ready_seeds_the_snapshot(db, configured_roles):
    guild = _fake_guild(_listed_invite("a", 2))
    bot = MagicMock()
    bot.get_guild = lambda gid: guild if gid == GUILD_ID else None

    await on_join.OnJoin(bot).on_ready()

    assert discord_invites.snapshot() == {"a": 2}


async def test_on_ready_survives_missing_manage_server(db, configured_roles):
    guild = _fake_guild()
    guild.invites = AsyncMock(side_effect=_forbidden())
    bot = MagicMock()
    bot.get_guild = lambda _gid: guild

    await on_join.OnJoin(bot).on_ready()  # must not raise

    assert discord_invites.snapshot() == {}
