"""Stage 14: observers, `~echo` and `~embed`.

The observer half is mostly about what *doesn't* happen. An observer has
no `Delegate` row, and that absence is what keeps them out of the guild
watch, the reconcile, the roster and the nickname enforcer — so most of
these assert on paths staying untouched, which is the only way to catch
a later change that starts treating them as a representative.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from hall_monitor.db.models import Delegate, Observer, PendingInvite
from hall_monitor.discord_bot.cogs.force import observer as force_observer
from hall_monitor.discord_bot.cogs.listeners import on_join
from hall_monitor.discord_bot.cogs.moderation import echo as echo_cog, embed as embed_cog
from hall_monitor.services import discord_invites, nicknames, role_bits

OBSERVER_ROLE_ID = 300


def _role(role_id, name):
    role = MagicMock()
    role.id = role_id
    role.name = name
    return role


class FakeGuild:
    def __init__(self):
        self.id = 1
        self.roles = [_role(OBSERVER_ROLE_ID, "Observer")]
        self.members = {}
        self.channel = MagicMock()
        invite = MagicMock()
        invite.code = "obs123"
        self.channel.create_invite = AsyncMock(return_value=invite)

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    def get_channel(self, _id):
        return self.channel

    def get_member(self, user_id):
        return self.members.get(user_id)

    def add_member(self, user_id, *, holding=()):
        member = MagicMock()
        member.id = user_id
        member.bot = False
        member.guild = self
        member.nick = None
        member.name = "Guest"
        member.roles = list(holding)
        member.add_roles = AsyncMock()
        member.edit = AsyncMock()
        self.members[user_id] = member
        return member


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for module in ("hall_monitor.services.nicknames", "hall_monitor.discord_bot.cogs.listeners.on_join", "hall_monitor.discord_bot.cogs.force.observer"):
        monkeypatch.setattr(f"{module}.settings.observer_role_id", OBSERVER_ROLE_ID)
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.force.observer.settings.welcome_channel_id", 2
    )
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.listeners.on_join.settings.discord_guild_id", 0
    )
    discord_invites.reset_snapshot()
    yield
    discord_invites.reset_snapshot()


@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def mojang(monkeypatch):
    """Resolve a username to a profile without touching Mojang."""
    known = {}

    async def fake(username, *, urgent=False):
        return known.get(username.lower())

    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.force.observer.resolve_profile", fake
    )
    return known


def _profile(username, uuid):
    profile = MagicMock()
    profile.username = username
    profile.uuid = uuid
    return profile


def _ctx(guild):
    ctx = MagicMock()
    ctx.guild = guild
    ctx.author = MagicMock()
    ctx.author.id = 999
    ctx.bot = MagicMock()
    ctx.bot.http.delete_invite = AsyncMock()
    ctx.reply = AsyncMock()
    ctx.send = AsyncMock()
    ctx.message.delete = AsyncMock()
    ctx.message.attachments = []
    return ctx


# --------------------------------------------------------------------------
# The sentinel
# --------------------------------------------------------------------------


def test_the_observer_sentinel_is_unreachable_from_minecraft():
    """A `HALL<NN>` code parses to 0–99, so a negative value can only ever
    have been written by `~force observer`."""
    assert role_bits.OBSERVER < 0
    assert role_bits.is_observer(role_bits.OBSERVER)
    assert not role_bits.is_observer(0), "HALL00 is a delegate who picked nothing"
    for value in range(100):
        assert not role_bits.is_observer(value)


def test_decoding_the_sentinel_raises_rather_than_returning_nothing():
    """Anything that treats an observer invite as a role set has to fail
    loudly — an empty set would silently apply no roles and look fine."""
    with pytest.raises(role_bits.UnknownRoleBit):
        role_bits.decode(role_bits.OBSERVER)


# --------------------------------------------------------------------------
# ~force observer
# --------------------------------------------------------------------------


async def test_inviting_an_observer_mints_a_week_long_invite(db, guild, mojang):
    """Ten minutes is right for a code typed in-game by somebody already
    at the keyboard, and useless for one a janitor has to hand over."""
    mojang["alice"] = _profile("Alice", "uuid-alice")

    reply = await force_observer.invite(_ctx(guild), "alice")

    row = await PendingInvite.get(mc_uuid="uuid-alice")
    assert row.roles_bits == role_bits.OBSERVER
    assert row.guild_tag == force_observer.OBSERVER_TAG
    assert row.expires_at is not None
    assert row.expires_at > datetime.now(timezone.utc) + timedelta(days=6)
    assert guild.channel.create_invite.await_args.kwargs["max_age"] == (
        discord_invites.OBSERVER_INVITE_MAX_AGE_SECONDS
    )
    assert "obs123" in reply


async def test_inviting_an_observer_refuses_a_registered_representative(
    db, guild, mojang
):
    mojang["alice"] = _profile("Alice", "uuid-alice")
    await Delegate.create(
        mc_uuid="uuid-alice", discord_user_id=5, guild_tag="VETS"
    )

    reply = await force_observer.invite(_ctx(guild), "alice")

    assert "already a registered representative" in reply
    assert not await PendingInvite.exists()


async def test_inviting_an_observer_refuses_when_the_role_is_unset(
    db, guild, mojang, monkeypatch
):
    """They'd arrive in the server with nothing at all, which is worse
    than not inviting them."""
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.force.observer.settings.observer_role_id", 0
    )
    mojang["alice"] = _profile("Alice", "uuid-alice")

    assert "OBSERVER_ROLE_ID" in await force_observer.invite(_ctx(guild), "alice")
    assert not await PendingInvite.exists()


async def test_inviting_an_unknown_username_says_so(db, guild, mojang):
    assert "isn't a Minecraft account" in await force_observer.invite(
        _ctx(guild), "nobody"
    )


async def test_revoking_an_observer_invite_removes_it(db, guild, mojang):
    mojang["alice"] = _profile("Alice", "uuid-alice")
    ctx = _ctx(guild)
    await force_observer.invite(ctx, "alice")

    reply = await force_observer.revoke(ctx, "alice")

    assert not await PendingInvite.exists()
    ctx.bot.http.delete_invite.assert_awaited()
    assert "revoked" in reply


async def test_revoking_refuses_to_touch_a_representative_invite(db, guild, mojang):
    """A typo here would cost somebody a re-verification, for a reason
    they'd have no way to see."""
    mojang["bob"] = _profile("Bob", "uuid-bob")
    await PendingInvite.create(
        mc_uuid="uuid-bob", guild_tag="VETS", roles_bits=5, discord_invite_code="x"
    )

    reply = await force_observer.revoke(_ctx(guild), "bob")

    assert "representative* invite" in reply
    assert await PendingInvite.exists()


async def test_revoking_nothing_says_so(db, guild, mojang):
    mojang["alice"] = _profile("Alice", "uuid-alice")
    assert "no outstanding invite" in await force_observer.revoke(_ctx(guild), "alice")


# --------------------------------------------------------------------------
# Arriving as an observer
# --------------------------------------------------------------------------


async def _redeem(guild, pending, user_id=7):
    """Drive `on_member_join` for a member arriving on ``pending``."""
    member = guild.add_member(user_id)
    cog = on_join.OnJoin(MagicMock())
    with_pending = AsyncMock(return_value=pending)
    original = discord_invites.resolve_used_invite
    discord_invites.resolve_used_invite = with_pending
    try:
        await cog.on_member_join(member)
    finally:
        discord_invites.resolve_used_invite = original
    return member


async def test_an_observer_gets_one_role_and_no_delegate_row(db, guild):
    """The absent row is what keeps them out of the guild watch, the
    reconcile, the roster and the nickname enforcer — without any of
    those learning what an observer is."""
    pending = await PendingInvite.create(
        mc_uuid="uuid-alice",
        mc_username="Alice",
        guild_tag=force_observer.OBSERVER_TAG,
        roles_bits=role_bits.OBSERVER,
        discord_invite_code="obs123",
    )

    member = await _redeem(guild, pending)

    added = [r.id for call in member.add_roles.await_args_list for r in call.args]
    assert added == [OBSERVER_ROLE_ID]
    assert not await Delegate.exists()
    assert not await PendingInvite.exists(), "consumed"
    member.edit.assert_not_awaited(), "no nickname tag — they represent nobody"


async def test_a_failed_observer_role_leaves_the_invite_for_the_sweep(db, guild):
    pending = await PendingInvite.create(
        mc_uuid="uuid-alice",
        guild_tag=force_observer.OBSERVER_TAG,
        roles_bits=role_bits.OBSERVER,
        discord_invite_code="obs123",
    )
    guild.members.clear()
    member = guild.add_member(7)
    member.add_roles.side_effect = discord.HTTPException(MagicMock(status=403), "no")

    cog = on_join.OnJoin(MagicMock())
    original = discord_invites.resolve_used_invite
    discord_invites.resolve_used_invite = AsyncMock(return_value=pending)
    try:
        await cog.on_member_join(member)
    finally:
        discord_invites.resolve_used_invite = original

    assert await PendingInvite.exists()


async def test_an_expelled_none_cannot_lock_observers_out(db, guild):
    """Observers carry the reserved `NONE` tag, so asking whether it's
    expelled is a question about nothing — and in the other order a stray
    `~force expel NONE` would turn away every observer, for a reason
    nobody would find."""
    from hall_monitor.services import expel

    await expel.record_ban("NONE", reason="somebody's typo")
    pending = await PendingInvite.create(
        mc_uuid="uuid-alice",
        mc_username="Alice",
        guild_tag=force_observer.OBSERVER_TAG,
        roles_bits=role_bits.OBSERVER,
        discord_invite_code="obs123",
    )

    member = await _redeem(guild, pending)

    added = [r.id for call in member.add_roles.await_args_list for r in call.args]
    assert added == [OBSERVER_ROLE_ID], "still let in"


# --------------------------------------------------------------------------
# An observer who becomes a chief
# --------------------------------------------------------------------------


@pytest.fixture
def wynncraft_says(monkeypatch):
    answer = {}

    async def fake(mc_uuid, *, urgent=False):
        return answer.get(mc_uuid)

    monkeypatch.setattr("hall_monitor.external.wynncraft.get_player_guild", fake)
    return answer


class _WynnGuild:
    def __init__(self, prefix, rank="CHIEF"):
        self.prefix = prefix
        self.rank = rank
        self.name = prefix


async def test_joining_records_the_binding(db, guild):
    """It has to outlive the invite: without it the bot can't say who an
    observer is, and can't tell that one who becomes a chief is already
    in the room."""
    pending = await PendingInvite.create(
        mc_uuid="uuid-alice",
        mc_username="Alice",
        guild_tag=force_observer.OBSERVER_TAG,
        roles_bits=role_bits.OBSERVER,
        discord_invite_code="obs123",
        invited_by_discord_user_id=999,
    )

    await _redeem(guild, pending)

    row = await Observer.get(mc_uuid="uuid-alice")
    assert row.discord_user_id == 7
    assert row.mc_username == "Alice"
    assert row.invited_by_discord_user_id == 999


async def test_an_observer_verifying_gets_told_rather_than_a_dead_invite(
    db, guild
):
    """The bug this record exists for. They pass the delegate guard,
    having no `Delegate` row, and the invite they'd be handed does
    nothing when clicked — an existing member joining fires no join
    event, so nothing consumes the `PendingInvite`."""
    await Observer.create(
        mc_uuid="uuid-alice", mc_username="Alice", discord_user_id=7
    )
    guild.add_member(7)

    with pytest.raises(discord_invites.AlreadyObserving) as caught:
        await discord_invites.mint_invite(
            "uuid-alice",
            "VETS",
            5,
            channel=guild.channel,
            discord_guild=guild,
        )

    assert caught.value.discord_user_id == 7
    guild.channel.create_invite.assert_not_awaited(), "no dead invite minted"


async def test_minting_an_observer_invite_is_not_blocked_by_the_guard(
    db, guild, mojang
):
    """`~force observer` mints *for* them, so the guard must not fire on
    its own command."""
    mojang["alice"] = _profile("Alice", "uuid-alice")
    await Observer.create(
        mc_uuid="uuid-alice", mc_username="Alice", discord_user_id=7
    )

    assert "obs123" in await force_observer.invite(_ctx(guild), "alice")


async def test_force_rep_promotes_an_observer(db, guild, wynncraft_says, monkeypatch):
    from hall_monitor.discord_bot.cogs.force import rep as force_rep
    from hall_monitor.services import major_guilds, transitions

    async def major(tag):
        return True

    monkeypatch.setattr(major_guilds, "is_major", major)
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.force.rep.settings.observer_role_id",
        OBSERVER_ROLE_ID,
    )

    async def settled(discord_guild, delegate):
        return transitions.Settlement(standing="delegate", guild_role="VETS", changes=2)

    monkeypatch.setattr(transitions, "settle_representative", settled)

    await Observer.create(
        mc_uuid="uuid-alice", mc_username="Alice", discord_user_id=7
    )
    member = guild.add_member(7, holding=[_role(OBSERVER_ROLE_ID, "Observer")])
    member.remove_roles = AsyncMock()
    wynncraft_says["uuid-alice"] = _WynnGuild("VETS")

    reply = await force_rep.repoint(_ctx(guild), member, "VETS")

    delegate = await Delegate.get(mc_uuid="uuid-alice")
    assert delegate.discord_user_id == 7 and delegate.guild_tag == "VETS"
    assert not await Observer.exists(), "the two states are exclusive"
    removed = [r.id for call in member.remove_roles.await_args_list for r in call.args]
    assert OBSERVER_ROLE_ID in removed
    assert "was an observer and now represents `VETS`" in reply


async def test_promotion_refuses_when_wynncraft_disagrees(db, guild, wynncraft_says):
    """Same authority as the re-point: the game, not the operator."""
    from hall_monitor.discord_bot.cogs.force import rep as force_rep

    await Observer.create(
        mc_uuid="uuid-alice", mc_username="Alice", discord_user_id=7
    )
    member = guild.add_member(7)
    wynncraft_says["uuid-alice"] = _WynnGuild("VETS", rank="RECRUITER")

    reply = await force_rep.repoint(_ctx(guild), member, "VETS")

    assert not await Delegate.exists()
    assert await Observer.exists(), "left exactly as they were"
    assert "chief or owner of any guild" in reply


async def test_standing_down_an_observer_who_already_joined(db, guild, mojang):
    """`~unforce observer` after they've used the invite means "stop being
    an observer", not "nothing to do"."""
    mojang["alice"] = _profile("Alice", "uuid-alice")
    await Observer.create(
        mc_uuid="uuid-alice", mc_username="Alice", discord_user_id=7
    )
    member = guild.add_member(7, holding=[_role(OBSERVER_ROLE_ID, "Observer")])
    member.remove_roles = AsyncMock()
    member.kick = AsyncMock()

    reply = await force_observer.revoke(_ctx(guild), "alice")

    assert not await Observer.exists()
    member.kick.assert_not_awaited(), "removing what they were given isn't removing them"
    assert "no longer an observer" in reply


async def test_leaving_drops_the_observer_record(db, guild):
    """Unlike a `Delegate` row, which the Hall keeps so a return costs no
    re-verification — the binding is about nobody once the account goes."""
    from hall_monitor.discord_bot.cogs.listeners import on_leave

    await Observer.create(
        mc_uuid="uuid-alice", mc_username="Alice", discord_user_id=7
    )
    member = guild.add_member(7)

    await on_leave.OnLeave(MagicMock()).on_member_remove(member)

    assert not await Observer.exists()


async def test_script_standing_names_an_observer(db, guild):
    from hall_monitor.discord_bot.cogs.admin.scripts import standing

    await Observer.create(
        mc_uuid="uuid-alice",
        mc_username="Alice",
        discord_user_id=7,
        invited_by_discord_user_id=999,
    )
    guild.add_member(7)
    ctx = _ctx(guild)

    await standing.main(ctx, "<@7>")

    body = ctx.reply.await_args.args[0]
    assert "observer" in body and "Alice" in body
    assert "`~force rep` promotes them" in body


async def test_nickname_enforcement_skips_an_observer(db, guild):
    """Belt and braces over the missing `Delegate` row: somebody who is
    *both* — a former representative given the observer role by hand —
    still keeps their own name."""
    await Delegate.create(
        mc_uuid="uuid-alice",
        mc_username="Alice",
        discord_user_id=7,
        guild_tag="VETS",
    )
    member = guild.add_member(7, holding=[_role(OBSERVER_ROLE_ID, "Observer")])

    assert await nicknames.enforce(member) is False
    member.edit.assert_not_awaited()


# --------------------------------------------------------------------------
# The invite lifetime the observer flow needed
# --------------------------------------------------------------------------


async def test_the_sweep_leaves_a_long_lived_invite_alone(db, monkeypatch):
    """Sweeping it on the default 45-minute TTL would revoke a live invite
    somebody is still holding, with nothing to say why it stopped."""
    monkeypatch.setattr(
        "hall_monitor.services.discord_invites.settings.pending_invite_ttl_minutes", 45
    )
    row = await PendingInvite.create(
        mc_uuid="uuid-alice",
        guild_tag="NONE",
        roles_bits=role_bits.OBSERVER,
        discord_invite_code="obs123",
        expires_at=datetime.now(timezone.utc) + timedelta(days=6),
    )
    await PendingInvite.filter(id=row.id).update(
        created_at=datetime.now(timezone.utc) - timedelta(hours=3)
    )

    assert await discord_invites.sweep_expired() == 0
    assert await PendingInvite.exists()


async def test_the_sweep_still_collects_an_ordinary_invite(db, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.services.discord_invites.settings.pending_invite_ttl_minutes", 45
    )
    row = await PendingInvite.create(
        mc_uuid="uuid-chief",
        guild_tag="VETS",
        roles_bits=5,
        discord_invite_code="mc123",
    )
    await PendingInvite.filter(id=row.id).update(
        created_at=datetime.now(timezone.utc) - timedelta(hours=3)
    )

    assert await discord_invites.sweep_expired() == 1


async def test_a_week_old_observer_invite_still_resolves_a_join(db, guild):
    """The freshness filter is per row. Judging an observer invite by the
    MC flow's ten minutes would read a live one as long expired and
    refuse to resolve the join."""
    row = await PendingInvite.create(
        mc_uuid="uuid-alice",
        guild_tag="NONE",
        roles_bits=role_bits.OBSERVER,
        discord_invite_code="obs123",
        expires_at=datetime.now(timezone.utc) + timedelta(days=5),
    )
    await PendingInvite.filter(id=row.id).update(
        created_at=datetime.now(timezone.utc) - timedelta(days=2)
    )
    discord_invites.note_minted("obs123")

    member = guild.add_member(7)
    member.guild.invites = AsyncMock(return_value=[])
    member.guild.vanity_url_code = None

    matched = await discord_invites.resolve_used_invite(member)

    assert matched is not None and matched.discord_invite_code == "obs123"


async def test_a_two_day_old_representative_invite_does_not_resolve(db, guild):
    """The default window still applies to rows without their own expiry —
    that invite expired hours ago, and matching it would bind a stranger's
    account to somebody else's UUID."""
    row = await PendingInvite.create(
        mc_uuid="uuid-chief",
        guild_tag="VETS",
        roles_bits=5,
        discord_invite_code="mc123",
    )
    await PendingInvite.filter(id=row.id).update(
        created_at=datetime.now(timezone.utc) - timedelta(days=2)
    )
    discord_invites.note_minted("mc123")

    member = guild.add_member(7)
    member.guild.invites = AsyncMock(return_value=[])
    member.guild.vanity_url_code = None

    assert await discord_invites.resolve_used_invite(member) is None


# --------------------------------------------------------------------------
# ~echo — silent and noisy
# --------------------------------------------------------------------------


async def _run(cog, command, ctx, content):
    await getattr(cog, command).callback(cog, ctx, content=content)


async def test_silent_echo_posts_then_deletes(db, guild):
    """Deleted only once the post is out — the other order loses what
    somebody just wrote if the send fails."""
    cog = echo_cog.Echo(MagicMock())
    ctx = _ctx(guild)

    await _run(cog, "silent_echo", ctx, "hello everyone")

    assert ctx.send.await_args.args[0] == "hello everyone"
    ctx.message.delete.assert_awaited()


async def test_silent_echo_rings_for_nobody(db, guild):
    """Mentions still render — `@Guild Hall Delegate` looks like itself
    and links through — they just don't notify."""
    cog = echo_cog.Echo(MagicMock())
    ctx = _ctx(guild)

    await _run(cog, "silent_echo", ctx, "@everyone @here <@1> <@&2>")

    mentions = ctx.send.await_args.kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False


async def test_noisy_echo_lets_everything_ring(db, guild):
    cog = echo_cog.Echo(MagicMock())
    ctx = _ctx(guild)

    await _run(cog, "noisy_echo", ctx, "@everyone come here")

    mentions = ctx.send.await_args.kwargs["allowed_mentions"]
    assert mentions.everyone is True
    assert mentions.users is True and mentions.roles is True


def test_echo_aliases_the_silent_one():
    """The short name anybody reaches for first must be the one that
    can't wake the room."""
    assert "echo" in echo_cog.Echo.silent_echo.aliases
    assert echo_cog.Echo.noisy_echo.aliases == []


async def test_echo_keeps_the_message_when_the_send_fails(db, guild):
    cog = echo_cog.Echo(MagicMock())
    ctx = _ctx(guild)
    ctx.send.side_effect = discord.HTTPException(MagicMock(status=500), "no")

    await _run(cog, "silent_echo", ctx, "hello")

    ctx.message.delete.assert_not_awaited()


async def test_echo_with_nothing_to_say_asks_for_something(db, guild):
    cog = echo_cog.Echo(MagicMock())
    ctx = _ctx(guild)

    await _run(cog, "silent_echo", ctx, "")

    ctx.send.assert_not_awaited()
    assert "give me something to say" in ctx.reply.await_args.args[0]


async def test_echo_carries_attachments_across(db, guild):
    cog = echo_cog.Echo(MagicMock())
    ctx = _ctx(guild)
    attachment = MagicMock()
    attachment.filename = "map.png"
    attachment.to_file = AsyncMock(return_value="FILE")
    ctx.message.attachments = [attachment]

    await _run(cog, "silent_echo", ctx, "")

    assert ctx.send.await_args.kwargs["files"] == ["FILE"]


async def test_an_attachment_that_fails_doesnt_lose_the_echo(db, guild):
    cog = echo_cog.Echo(MagicMock())
    ctx = _ctx(guild)
    bad = MagicMock()
    bad.filename = "map.png"
    bad.to_file = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "no")
    )
    ctx.message.attachments = [bad]

    await _run(cog, "silent_echo", ctx, "still says this")

    assert ctx.send.await_args.args[0] == "still says this"


# --------------------------------------------------------------------------
# ~embed — silent and noisy
# --------------------------------------------------------------------------


def _embed(content, **kwargs):
    return embed_cog.parse(content, **kwargs)[0]


def test_embed_parses_quoted_fields():
    built = _embed(
        'title="Verification" desc="Head to hall.wynnvets.org/join" colour=#5865F2'
    )

    assert built.title == "Verification"
    assert built.description == "Head to hall.wynnvets.org/join"
    assert built.colour.value == 0x5865F2


def test_embed_treats_bare_text_as_the_description():
    """Somebody's first use of this will not have the syntax in front of
    them, and an error would teach them nothing."""
    assert _embed("Just a sentence").description == "Just a sentence"


def test_embed_accepts_colour_by_name_and_the_american_spelling():
    assert _embed("color=red desc=x").colour == discord.Colour.red()


def test_embed_accepts_description_as_an_alias():
    assert _embed('description="hello"').description == "hello"


def test_embed_rejects_an_unknown_colour_by_name():
    with pytest.raises(embed_cog.BadEmbed, match="isn't a colour"):
        _embed("colour=chartreuse desc=x")


def test_embed_with_nothing_in_it_is_refused():
    with pytest.raises(embed_cog.BadEmbed, match="something to show"):
        _embed("   ")


def test_embed_keeps_newlines_in_a_quoted_value():
    assert _embed('desc="line one\nline two"').description == "line one\nline two"


def test_embed_leaves_an_unrecognised_key_in_the_prose():
    """`foo=bar` isn't a field, so it belongs in what the panel says
    rather than being silently dropped."""
    assert "foo=bar" in _embed("foo=bar and some words").description


def test_ping_goes_in_the_content_because_embeds_never_notify():
    """Discord raises notifications from a message's *content*; an embed's
    body is not content, so a `@here` in a description rings for nobody
    whatever `allowed_mentions` says. `ping=` is the only mechanism that
    works, which is why `~noisy_embed` has one."""
    built, ping = embed_cog.parse(
        'ping="@here" title="Maintenance" desc="Back in an hour."',
        allow_ping=True,
    )

    assert ping == "@here"
    assert "@here" not in (built.description or "")
    assert built.title == "Maintenance"


def test_a_silent_embed_refuses_ping_rather_than_dropping_it():
    """Ignoring it would leave a janitor believing they'd notified the
    room — the exact shape of failure §12.3 is a list of."""
    with pytest.raises(embed_cog.BadEmbed, match="monitor-only"):
        embed_cog.parse('ping="@here" desc="x"', allow_ping=False)


def test_embed_aliases_the_silent_one():
    assert "embed" in embed_cog.Embed.silent_embed.aliases
    assert embed_cog.Embed.noisy_embed.aliases == []


async def test_silent_embed_posts_then_deletes(db, guild):
    cog = embed_cog.Embed(MagicMock())
    ctx = _ctx(guild)

    await _run(cog, "silent_embed", ctx, 'title="Notice" desc="hello"')

    assert ctx.send.await_args.kwargs["embed"].title == "Notice"
    assert ctx.send.await_args.kwargs["allowed_mentions"].everyone is False
    ctx.message.delete.assert_awaited()


async def test_noisy_embed_sends_the_ping_as_content(db, guild):
    cog = embed_cog.Embed(MagicMock())
    ctx = _ctx(guild)

    await _run(cog, "noisy_embed", ctx, 'ping="@here" desc="hello"')

    assert ctx.send.await_args.args[0] == "@here"
    assert ctx.send.await_args.kwargs["allowed_mentions"].everyone is True


async def test_embed_relays_discords_own_complaint(db, guild):
    """Discord validates embeds server-side, and its reason is far more
    use to the author than "that broke on my end"."""
    cog = embed_cog.Embed(MagicMock())
    ctx = _ctx(guild)
    failure = discord.HTTPException(MagicMock(status=400), "no")
    failure.text = "Not a well formed URL"
    ctx.send.side_effect = failure

    await _run(cog, "silent_embed", ctx, 'desc="x" url=notaurl')

    assert "Not a well formed URL" in ctx.reply.await_args.args[0]
    ctx.message.delete.assert_not_awaited()
