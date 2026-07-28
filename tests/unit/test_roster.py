"""The Current Guilds channel: what it lists, in what order, and how a
sync gets the channel to say it without reordering messages it can't."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from hall_monitor.db.models import (
    Delegate,
    ForceOverride,
    GuildContact,
    GuildEmote,
    MajorGuildCache,
    RosterMessage,
)
from hall_monitor.services import delegate_registry, roster

CHANNEL_ID = 555


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.services.roster.settings.roster_channel_id", CHANNEL_ID
    )


class FakeEmoji:
    def __init__(self, name: str, emoji_id: int = 1) -> None:
        self.name = name
        self.id = emoji_id

    def __str__(self) -> str:
        return f"<:{self.name}:{self.id}>"


class FakeMessage:
    _next_id = 1000

    def __init__(self, content: str, *, channel=None) -> None:
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.content = content
        self.channel = channel
        self.deleted = False
        self.edits: list[str] = []

    async def edit(self, *, content: str, allowed_mentions=None) -> None:
        self.edits.append(content)
        self.allowed_mentions = allowed_mentions
        self.content = content

    async def delete(self) -> None:
        self.deleted = True
        if self.channel is not None:
            self.channel.messages.remove(self)


class FakeChannel:
    def __init__(self) -> None:
        self.messages: list[FakeMessage] = []
        self.sent: list[str] = []

    def history(self, *, limit=None, oldest_first=True):
        snapshot = list(self.messages)

        async def walk():
            for message in snapshot:
                yield message

        return walk()

    async def send(self, content: str, *, allowed_mentions=None) -> FakeMessage:
        self.sent.append(content)
        message = FakeMessage(content, channel=self)
        message.allowed_mentions = allowed_mentions
        self.messages.append(message)
        return message

    def seed(self, content: str) -> FakeMessage:
        message = FakeMessage(content, channel=self)
        self.messages.append(message)
        return message


class FakeGuild:
    def __init__(self, *, emojis=(), channel=None) -> None:
        self.emojis = list(emojis)
        self.channel = FakeChannel() if channel is None else channel
        self.members: dict[int, MagicMock] = {}

    def get_channel(self, channel_id):
        return self.channel if channel_id == CHANNEL_ID else None

    def get_member(self, user_id):
        return self.members.get(user_id)

    def add_member(self, user_id):
        member = MagicMock()
        member.id = user_id
        self.members[user_id] = member
        return member


async def _cache(tag, *, major=True, name=None, rank=None):
    return await MajorGuildCache.create(
        guild_tag=tag,
        is_major=major,
        signals_json="{}",
        guild_name=name,
        level_rank=rank,
    )


async def _delegate(uuid, user_id, tag, *, username="player", currently=None):
    return await Delegate.create(
        mc_uuid=uuid,
        discord_user_id=user_id,
        mc_username=username,
        guild_tag=tag,
        current_guild_tag=currently or tag,
    )


# --------------------------------------------------------------------------
# Which guilds are listed, and in what order
# --------------------------------------------------------------------------


async def test_guilds_are_ordered_by_level_board_rank(db):
    await _cache("AAAA", rank=12)
    await _cache("VETS", rank=3)
    await _cache("SEQ", rank=7)

    assert [one.tag for one in await roster.listed_guilds()] == ["VETS", "SEQ", "AAAA"]


async def test_unranked_guilds_sort_last_alphabetically(db):
    """A guild off the level board still belongs on the roster; it just
    has nothing to sort by."""
    await _cache("VETS", rank=3)
    await _cache("ZZZ")
    await _cache("BBB")

    assert [one.tag for one in await roster.listed_guilds()] == ["VETS", "BBB", "ZZZ"]


async def test_only_major_guilds_are_listed(db):
    await _cache("VETS", major=True)
    await _cache("WYNN", major=False)

    assert [one.tag for one in await roster.listed_guilds()] == ["VETS"]


async def test_a_forced_guild_appears_before_its_first_sweep(db):
    """`~force major NEWG 3mo` has to show up now — an entry that waits
    an hour reads as the command not having worked."""
    await ForceOverride.create(kind="major", subject="NEWG", expires_at=None)

    assert [one.tag for one in await roster.listed_guilds()] == ["NEWG"]


async def test_an_expired_force_doesnt_keep_a_guild_listed(db):
    from datetime import datetime, timedelta, timezone

    await _cache("OLDG", major=False)
    await ForceOverride.create(
        kind="major",
        subject="OLDG",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    assert await roster.listed_guilds() == []


async def test_a_guild_is_listed_once_whatever_its_spelling(db):
    """`VETS` and `vets` are one guild; printing both would have the room
    reading two entries for the same people."""
    await _cache("VETS", rank=3, name="Returners")
    await ForceOverride.create(kind="major", subject="vets", expires_at=None)

    listed = await roster.listed_guilds()

    assert [one.tag for one in listed] == ["VETS"]
    assert listed[0].name == "Returners", "the cache's spelling wins for display"


async def test_a_guild_with_no_cached_name_falls_back_to_its_tag(db):
    await _cache("VETS")

    assert (await roster.listed_guilds())[0].name == "VETS"


# --------------------------------------------------------------------------
# What one guild's block says
# --------------------------------------------------------------------------


async def test_a_block_matches_the_reference_layout(db):
    guild = FakeGuild(emojis=[FakeEmoji("VETS", 77)])
    await GuildEmote.create(guild_tag="VETS", discord_emoji_id=77, image_hash="h")
    delegate = await _delegate("uuid-a", 1, "VETS", username="wenweia")
    guild.add_member(1)
    await GuildContact.create(guild_tag="VETS", role="ownership", delegate=delegate)

    block = await roster.render_guild(
        guild, "VETS", "Returners", await roster.minted_emotes()
    )

    assert block.splitlines() == [
        "<:VETS:77> **Returners** (`VETS`)",
        "- **Ownership Contact**: <@1> (`wenweia`)",
        "- **Events Contact**: `unclaimed`",
        "- **Warring Contact**: `unclaimed`",
        "- **Housing Contact**: `unclaimed`",
    ]


async def test_a_guild_without_its_own_emote_wears_the_placeholder(db):
    guild = FakeGuild(emojis=[FakeEmoji(roster.PLACEHOLDER_EMOTE_NAME)])

    block = await roster.render_guild(guild, "VETS", "Returners")

    assert block.startswith(f"<:{roster.PLACEHOLDER_EMOTE_NAME}:1> ")


async def test_our_banner_is_found_by_id_not_by_name(db):
    """`emote_slots` may rewrite a tag to satisfy Discord's naming rules,
    and the name belongs to whoever last edited it either way."""
    guild = FakeGuild(emojis=[FakeEmoji("A_B", 77), FakeEmoji(roster.PLACEHOLDER_EMOTE_NAME, 5)])
    await GuildEmote.create(guild_tag="A B", discord_emoji_id=77, image_hash="h")

    block = await roster.render_guild(
        guild, "A B", "Spaced Out", await roster.minted_emotes()
    )

    assert block.startswith("<:A_B:77> ")


async def test_a_guild_outside_the_emote_budget_wears_the_placeholder(db):
    """Far more major guilds than emote slots, so most of the roster
    sits on the shared placeholder permanently."""
    guild = FakeGuild(emojis=[FakeEmoji(roster.PLACEHOLDER_EMOTE_NAME, 5)])

    block = await roster.render_guild(guild, "VETS", "Returners", {})

    assert block.startswith(f"<:{roster.PLACEHOLDER_EMOTE_NAME}:5> ")


async def test_a_server_missing_the_placeholder_still_renders(db):
    """A missing emote must not be the thing that stops the roster."""
    guild = FakeGuild()

    assert (await roster.render_guild(guild, "VETS", "Returners")).startswith(
        roster.FALLBACK_EMOTE
    )


async def test_a_contact_who_left_the_server_reads_as_unclaimed(db):
    guild = FakeGuild()  # no members at all
    delegate = await _delegate("uuid-a", 1, "VETS")
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    block = await roster.render_guild(guild, "VETS", "Returners")

    assert "- **Events Contact**: `unclaimed`" in block


async def test_a_contact_who_moved_guilds_reads_as_unclaimed(db):
    """They keep the slot and lose the role (§6). Naming them here would
    point the room at somebody who has left."""
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1, "VETS", currently="OTHR")
    guild.add_member(1)
    await GuildContact.create(guild_tag="VETS", role="warring", delegate=delegate)

    block = await roster.render_guild(guild, "VETS", "Returners")

    assert "- **Warring Contact**: `unclaimed`" in block


async def test_a_repointed_contact_is_named_for_the_guild_they_speak_for(db):
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1, "VETS", username="wen")
    guild.add_member(1)
    await GuildContact.create(guild_tag="ANO", role="housing", delegate=delegate)
    await delegate_registry.set_forced_guild(1, "ANO", None)

    block = await roster.render_guild(guild, "ANO", "Anonymous")

    assert "- **Housing Contact**: <@1> (`wen`)" in block


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def test_a_guild_is_never_split_across_messages():
    blocks = [("A", "a" * 60), ("B", "b" * 60), ("C", "c" * 60)]

    chunks = roster.chunk(blocks, limit=130)

    assert [one.tags for one in chunks] == [("A", "B"), ("C",)]
    assert all(len(one.content) <= 130 for one in chunks)


def test_chunks_keep_the_blank_line_between_guilds():
    chunks = roster.chunk([("A", "aaa"), ("B", "bbb")], limit=100)

    assert chunks[0].content == "aaa\n\nbbb"


def test_an_oversized_block_is_truncated_rather_than_dropped():
    """Not reachable with four contact lines — but a message Discord
    rejects would end the roster at that guild."""
    chunks = roster.chunk([("A", "a" * 200)], limit=100)

    assert len(chunks) == 1 and len(chunks[0].content) == 100


def test_an_empty_roster_is_no_messages():
    assert roster.chunk([]) == []


# --------------------------------------------------------------------------
# Syncing the channel
# --------------------------------------------------------------------------


async def _one_guild(guild, tag="VETS", *, rank=1):
    await _cache(tag, rank=rank, name=f"{tag} Guild")
    return guild


async def test_the_first_run_posts_the_roster_and_clears_the_channel(db):
    """Existing messages go: the bot is the only poster here by design."""
    guild = FakeGuild()
    stale = guild.channel.seed("somebody chatting in the wrong channel")
    await _one_guild(guild)

    summary = await roster.sync_channel(guild)

    assert stale.deleted
    assert summary.created == 1 and summary.purged == 1
    assert await RosterMessage.all().count() == 1
    assert "**VETS Guild** (`VETS`)" in guild.channel.messages[0].content


async def test_a_second_run_with_nothing_to_say_makes_no_edits(db):
    """It runs hourly. An "edited" marker appearing on the whole roster
    every hour is the visible cost of getting this wrong."""
    guild = FakeGuild()
    await _one_guild(guild)
    await roster.sync_channel(guild)
    posted = guild.channel.messages[0]

    summary = await roster.sync_channel(guild)

    assert summary == roster.RosterSync(guilds=1, messages=1)
    assert posted.edits == []


async def test_a_new_contact_edits_the_message_in_place(db):
    guild = FakeGuild()
    await _one_guild(guild)
    await roster.sync_channel(guild)
    posted = guild.channel.messages[0]

    delegate = await _delegate("uuid-a", 1, "VETS", username="wenweia")
    guild.add_member(1)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)
    summary = await roster.sync_channel(guild)

    assert summary.edited == 1 and summary.created == 0
    assert guild.channel.messages == [posted], "same message, not a new one"
    assert "<@1> (`wenweia`)" in posted.content


async def test_a_growing_roster_appends_without_touching_what_fits(db, monkeypatch):
    monkeypatch.setattr(roster, "MESSAGE_LIMIT", 200)
    guild = FakeGuild()
    await _cache("AAA", rank=1)
    await roster.sync_channel(guild)
    first = guild.channel.messages[0]

    await _cache("BBB", rank=2)
    summary = await roster.sync_channel(guild)

    assert summary.messages == 2 and summary.created == 1
    assert first.edits == [], "the first message didn't change"
    assert await RosterMessage.all().count() == 2


async def test_a_shrinking_roster_deletes_the_tail(db, monkeypatch):
    monkeypatch.setattr(roster, "MESSAGE_LIMIT", 200)
    guild = FakeGuild()
    await _cache("AAA", rank=1)
    gone = await _cache("BBB", rank=2)
    await roster.sync_channel(guild)
    assert len(guild.channel.messages) == 2
    tail = guild.channel.messages[1]

    await gone.delete()
    summary = await roster.sync_channel(guild)

    assert summary.deleted == 1 and tail.deleted
    assert [row.position for row in await RosterMessage.all()] == [0]


async def test_a_hand_deleted_message_is_rebuilt_in_order(db, monkeypatch):
    """Discord can't insert a message above an existing one, so the only
    way to keep the order is to re-send everything from the gap down."""
    monkeypatch.setattr(roster, "MESSAGE_LIMIT", 200)
    guild = FakeGuild()
    for rank, tag in enumerate(("AAA", "BBB", "CCC"), start=1):
        await _cache(tag, rank=rank)
    await roster.sync_channel(guild)
    first, middle, last = guild.channel.messages
    await middle.delete()

    summary = await roster.sync_channel(guild)

    assert summary.created == 2 and last.deleted
    assert guild.channel.messages[0] is first, "the intact prefix was kept"
    contents = [message.content for message in guild.channel.messages]
    assert ["AAA", "BBB", "CCC"] == [
        tag for tag in ("AAA", "BBB", "CCC") if any(tag in one for one in contents)
    ]
    assert "BBB" in contents[1] and "CCC" in contents[2]


async def test_messages_out_of_order_on_screen_are_rebuilt(db, monkeypatch):
    """The channel is what readers see, so it wins over our bookkeeping."""
    monkeypatch.setattr(roster, "MESSAGE_LIMIT", 200)
    guild = FakeGuild()
    await _cache("AAA", rank=1)
    await _cache("BBB", rank=2)
    await roster.sync_channel(guild)
    guild.channel.messages.reverse()

    summary = await roster.sync_channel(guild)

    # The first message is still usable where it is; everything after the
    # point where the screen stops agreeing gets re-sent below it.
    assert (summary.created, summary.deleted) == (1, 1)
    assert "AAA" in guild.channel.messages[0].content
    assert "BBB" in guild.channel.messages[1].content


async def test_an_untracked_message_of_ours_is_swept_up(db):
    """The residue of a pass that died between sending and recording —
    otherwise it sits above the roster forever."""
    guild = FakeGuild()
    await _one_guild(guild)
    await roster.sync_channel(guild)
    orphan = guild.channel.seed("<:x:1> **Ghost Guild** (`GHST`)")

    summary = await roster.sync_channel(guild)

    assert summary.purged == 1 and orphan.deleted


async def test_no_configured_channel_is_a_quiet_skip(db, monkeypatch):
    monkeypatch.setattr("hall_monitor.services.roster.settings.roster_channel_id", 0)

    assert await roster.sync_channel(FakeGuild()) is None


async def test_a_channel_that_isnt_in_the_guild_is_a_quiet_skip(db):
    guild = FakeGuild()
    guild.get_channel = lambda channel_id: None

    assert await roster.sync_channel(guild) is None


async def test_a_failed_send_stops_rather_than_posting_out_of_order(db, monkeypatch):
    monkeypatch.setattr(roster, "MESSAGE_LIMIT", 200)
    guild = FakeGuild()
    await _cache("AAA", rank=1)
    await _cache("BBB", rank=2)
    guild.channel.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "nope")
    )

    summary = await roster.sync_channel(guild)

    assert summary.created == 0
    assert await RosterMessage.all().count() == 0


async def test_the_roster_never_pings_the_people_on_it(db):
    """It redraws on every join, leave, force and hourly sweep. Four
    contacts a guild getting notified each time is unusable."""
    guild = FakeGuild()
    await _one_guild(guild)
    delegate = await _delegate("uuid-a", 1, "VETS")
    guild.add_member(1)
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    await roster.sync_channel(guild)
    posted = guild.channel.messages[0]

    assert "<@1>" in posted.content, "still a real mention, so it links"
    assert posted.allowed_mentions is roster.SILENT

    # And on the edit path too — an edit that omits it falls back to the
    # default and pings, which is the half of this that's easy to miss.
    await GuildContact.create(guild_tag="VETS", role="housing", delegate=delegate)
    await roster.sync_channel(guild)

    assert posted.edits and posted.allowed_mentions is roster.SILENT


# --------------------------------------------------------------------------
# A representative leaving the server
# --------------------------------------------------------------------------


def _leaving(guild, user_id):
    member = MagicMock()
    member.id = user_id
    member.bot = False
    member.guild = guild
    return member


async def test_a_leaving_representative_is_marked_left(db, monkeypatch):
    """Otherwise the hourly watch spends a Wynncraft request a sweep on
    somebody who's gone, and the reconcile counts them as a presence."""
    from hall_monitor.discord_bot.cogs.listeners import on_leave

    monkeypatch.setattr(roster, "request_sync", lambda guild: None)
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.listeners.on_leave.settings.discord_guild_id", 0
    )
    guild = FakeGuild()
    await _delegate("uuid-a", 1, "VETS")

    await on_leave.OnLeave(MagicMock()).on_member_remove(_leaving(guild, 1))

    assert (await Delegate.get(mc_uuid="uuid-a")).left_at is not None


async def test_a_leaving_representative_keeps_their_contact_slots(db, monkeypatch):
    """Rows never move (§6) — coming back costs no re-verification, and
    the display side already reads an absent holder as unclaimed."""
    from hall_monitor.discord_bot.cogs.listeners import on_leave

    monkeypatch.setattr(roster, "request_sync", lambda guild: None)
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.listeners.on_leave.settings.discord_guild_id", 0
    )
    guild = FakeGuild()
    delegate = await _delegate("uuid-a", 1, "VETS")
    await GuildContact.create(guild_tag="VETS", role="events", delegate=delegate)

    await on_leave.OnLeave(MagicMock()).on_member_remove(_leaving(guild, 1))

    assert await GuildContact.filter(guild_tag="VETS").count() == 1


async def test_a_leaving_guest_is_left_alone(db, monkeypatch):
    """Staff and visitors have no row to close and no roster to redraw."""
    from hall_monitor.discord_bot.cogs.listeners import on_leave

    asked = []
    monkeypatch.setattr(roster, "request_sync", lambda guild: asked.append(guild))
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.listeners.on_leave.settings.discord_guild_id", 0
    )

    await on_leave.OnLeave(MagicMock()).on_member_remove(_leaving(FakeGuild(), 999))

    assert asked == []


# --------------------------------------------------------------------------
# The debounced request
# --------------------------------------------------------------------------


async def _force_tree():
    from hall_monitor.discord_bot import build_bot

    bot = build_bot()
    await bot.load_extension("hall_monitor.discord_bot.cogs.force")
    return bot


def _after_invoke_ctx(guild, *, failed=False):
    ctx = MagicMock()
    ctx.guild = guild
    ctx.command_failed = failed
    ctx.bot._after_invoke = None  # the global hook discord.py calls last
    return ctx


async def test_every_force_subcommand_redraws_the_roster(db, monkeypatch):
    """`~force guild` and `~force major` shipped without a hook and the
    roster silently went stale, so one was put on the group — where it
    **never fired once**. These sub-commands are attached to the group
    inside `register()`, so they aren't class attributes, so they aren't
    in `__cog_commands__`, so `Cog._inject` never sets their `.cog`, and
    `call_after_hooks` only reaches the cog hook `if cog is not None`.
    Three `~force assign`s in a row left the roster showing the state
    before all of them, each reporting success.

    The version of this test that let that through called
    `ForceGroup.cog_after_invoke(ctx)` **directly**: it proved the body
    worked, never that discord.py would call it. So this one goes through
    the real command tree and the library's own hook dispatch, which is
    the part that was broken.
    """
    bot = await _force_tree()
    asked = []
    monkeypatch.setattr(roster, "request_sync", lambda guild: asked.append(guild))
    guild = FakeGuild()

    subcommands = [
        *bot.get_command("force").walk_commands(),
        *bot.get_command("unforce").walk_commands(),
    ]
    assert subcommands, "the tree loaded"
    for command in subcommands:
        await command.call_after_hooks(_after_invoke_ctx(guild))

    assert len(asked) == len(subcommands)


async def test_a_failed_force_command_doesnt_redraw(db, monkeypatch):
    """Nothing moved, so there's nothing to publish — and after-hooks run
    in a `finally`, so this path is reached on failure too."""
    bot = await _force_tree()
    asked = []
    monkeypatch.setattr(roster, "request_sync", lambda guild: asked.append(guild))

    await bot.get_command("force").get_command("assign").call_after_hooks(
        _after_invoke_ctx(FakeGuild(), failed=True)
    )

    assert asked == []


async def test_a_burst_of_requests_costs_one_sync(db, monkeypatch):
    """A verification claims four slots and sets a nickname; each is a
    reason to redraw, and one redraw is the right number."""
    monkeypatch.setattr(roster, "SYNC_DELAY_SECONDS", 0)
    guild = FakeGuild()
    await _one_guild(guild)
    passes = 0
    real = roster.sync_channel

    async def counting(discord_guild):
        nonlocal passes
        passes += 1
        return await real(discord_guild)

    monkeypatch.setattr(roster, "sync_channel", counting)

    for _ in range(5):
        roster.request_sync(guild)
    await roster.wait_for_pending_sync()

    assert passes == 1
    assert len(guild.channel.messages) == 1
