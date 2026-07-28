"""The Current Guilds channel — a bot-maintained list of the whole Hall.

One channel, posts by nobody but this bot, listing every guild currently
notable with its four contacts. It's the answer to "who do I ask about
housing in Sequoia?" without anyone having to know a delegate first, and
it's the one place the Hall is legible from outside a DM.

Like ``services/transitions.py``, this is a **reconcile** rather than a
set of edge-triggered edits: :func:`sync_channel` renders what the channel
*should* say and makes it say that. Nothing tracks "what changed"; there's
no event that can be missed, and a channel someone mangled by hand heals
on the next pass.

Three things follow from Discord's message model and shape everything
here:

- **Messages can't be reordered or inserted.** A roster spanning eight
  messages is eight messages in send order, so replacing the third means
  re-sending the third onward. :func:`sync_channel` edits the longest
  intact prefix in place and rebuilds from the first gap.
- **2000 characters.** Guilds are packed into messages without ever
  splitting one across a boundary, so a guild gaining a contact can push
  everything below it into different messages — which is exactly the case
  the prefix-and-rebuild walk above is for.
- **Editing is cheap, sending is not.** A message whose content already
  matches is left strictly alone: no edit, no request, and no "edited"
  marker appearing on the whole roster every hour.

The ordering is Wynnpool's guild-level board, highest first, with guilds
absent from it after the ranked ones alphabetically. Both the rank and
the guild's full name are read from ``NotabilityCache``, written by the
hourly sweep — so rendering the roster costs no third-party request at
all, and the ordering is exactly as fresh as the notability behind it.
"""

import asyncio
import logging
from dataclasses import dataclass

import discord

from hall_monitor.config import settings
from hall_monitor.db.models import Delegate, NotabilityCache, RosterMessage
from hall_monitor.services import (
    contacts,
    delegate_registry,
    guild_tag as tags,
    notability,
)

logger = logging.getLogger(__name__)

# The reference layout puts ownership first — it's the slot that speaks
# for the guild rather than for one of its activities.
ORDERED_CONTACT_ROLES = ("ownership", "events", "warring", "housing")

# Discord's cap on a message body.
MESSAGE_LIMIT = 2000

# Enough messages for several hundred guilds; a roster longer than this
# has a problem the sync can't fix by scrolling further.
HISTORY_LIMIT = 200

# The emote a guild wears until Stage 12 renders its banner. Uploaded to
# the server by hand under this name; a server without it falls through
# to a unicode flag, because a missing emote must not stop the roster.
PLACEHOLDER_EMOTE_NAME = "Empty_Banner"
FALLBACK_EMOTE = "\N{WAVING WHITE FLAG}"

UNCLAIMED = "`unclaimed`"

# Contacts are named with a real `<@id>` so the entry stays a live link to
# the person — but the roster is redrawn on every join, leave, force and
# hourly sweep, and none of those are a reason to notify four people per
# guild. This suppresses the *notification* only: the mention still
# renders as their name, in the usual highlight, and still clicks through.
# It has to be passed on `edit` as well as `send`, because an edit that
# omits it falls back to the default and pings.
SILENT = discord.AllowedMentions.none()

# Guilds with no level-board rank sort after every guild that has one.
_UNRANKED = 10**6

# How long a `request_sync` waits before running, so a burst of joins or
# a command that moves four slots at once costs one pass rather than four.
SYNC_DELAY_SECONDS = 5.0

_pending: asyncio.Task | None = None


@dataclass(frozen=True)
class RosterChunk:
    """One message's worth of the roster, and which guilds went into it."""

    content: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class RosterSync:
    """What one pass over the channel actually did."""

    guilds: int = 0
    messages: int = 0
    created: int = 0
    edited: int = 0
    deleted: int = 0
    purged: int = 0

    def line(self) -> str:
        """One-line report for the log and `~script roster`."""
        work = ", ".join(
            f"{count} {label}"
            for label, count in (
                ("created", self.created),
                ("edited", self.edited),
                ("deleted", self.deleted),
                ("purged", self.purged),
            )
            if count
        )
        return (
            f"{self.guilds} guilds across {self.messages} message(s); "
            f"{work or 'nothing to do'}"
        )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ListedGuild:
    """A guild the roster lists, and what it needs to print it."""

    tag: str
    name: str
    rank: int | None


async def listed_guilds() -> list[ListedGuild]:
    """Every guild the roster lists, in the order it lists them.

    Candidates come from the notability cache and from live ``notable``
    force overrides — a guild a janitor has just forced has no cache row
    until the next sweep, and waiting an hour to appear would read as the
    command not having worked.

    Deliberately *only* those two sources, and answered in bulk rather
    than by asking :func:`notability.is_notable` per guild: that call
    falls through to a live evaluation on a cache miss, and a roster
    render is not the place to discover a guild needs twenty leaderboard
    fetches. Two queries settle the lot.
    """
    # Keyed by comparison key throughout: `VETS` and `vets` are one guild,
    # and listing both would print it twice. The first spelling seen wins
    # for display, cache before override, since the cache carries
    # Wynncraft's own capitalisation and the override carries a janitor's.
    cached: dict[str, dict] = {}
    for row in await NotabilityCache.all().values(
        "guild_tag", "guild_name", "level_rank", "is_notable"
    ):
        cached.setdefault(tags.normalise(row["guild_tag"]), row)
    forced = await notability.active_notable_overrides()

    listed = []
    for folded in {**forced, **cached}:
        row = cached.get(folded)
        if not (folded in forced or (row and row["is_notable"])):
            continue
        tag = row["guild_tag"] if row else forced[folded]
        listed.append(
            ListedGuild(
                tag=tag,
                name=(row and row["guild_name"]) or tag,
                rank=row["level_rank"] if row else None,
            )
        )
    listed.sort(key=lambda one: (_UNRANKED if one.rank is None else one.rank, one.tag.upper()))
    return listed


async def render_guild(
    discord_guild: discord.Guild, tag: str, name: str
) -> str:
    """The block for one guild: banner emote, name, tag, four contacts."""
    lines = [f"{emote_for(discord_guild, tag)} **{name}** (`{tag}`)"]
    holders = await contacts.current_contacts_for_guild(tag, discord_guild)
    for role in ORDERED_CONTACT_ROLES:
        lines.append(
            f"- **{role.capitalize()} Contact**: "
            f"{await _holder(holders.get(role), tag)}"
        )
    return "\n".join(lines)


async def render_full_roster(discord_guild: discord.Guild) -> list[RosterChunk]:
    """The whole roster, packed into message-sized chunks in order."""
    blocks = [
        (one.tag, await render_guild(discord_guild, one.tag, one.name))
        for one in await listed_guilds()
    ]
    return chunk(blocks)


def chunk(
    blocks: list[tuple[str, str]], limit: int | None = None
) -> list[RosterChunk]:
    """Pack rendered guild blocks into messages, never splitting a guild.

    A guild's four contact lines belong together — half of them at the
    bottom of one message and half at the top of the next reads as two
    different guilds. The cost is that one guild gaining a contact can
    reflow every message below it, which :func:`sync_channel` handles by
    rebuilding from the first message that changed shape.
    """
    limit = limit or MESSAGE_LIMIT
    messages: list[RosterChunk] = []
    current: list[str] = []
    current_tags: list[str] = []
    length = 0

    for tag, block in blocks:
        if len(block) > limit:
            # Not reachable with four contact lines, but a truncated entry
            # beats a message Discord rejects and a roster that stops here.
            logger.warning("roster: %s renders longer than a message; truncating", tag)
            block = block[:limit]
        separator = 2 if current else 0  # the blank line between guilds
        if current and length + separator + len(block) > limit:
            messages.append(
                RosterChunk("\n\n".join(current), tuple(current_tags))
            )
            current, current_tags, length = [], [], 0
            separator = 0
        current.append(block)
        current_tags.append(tag)
        length += separator + len(block)

    if current:
        messages.append(RosterChunk("\n\n".join(current), tuple(current_tags)))
    return messages


def emote_for(discord_guild: discord.Guild, tag: str) -> str:
    """The guild's banner emote, the shared placeholder, or a plain flag.

    Stage 12 mints the per-guild ones; until then every guild wears the
    placeholder. Resolved by *name* rather than from a stored ID because
    the emote is the server's to re-upload, and a roster that breaks when
    somebody does that would be worse than one that finds it again.
    """
    for name in (tag, PLACEHOLDER_EMOTE_NAME):
        for emoji in discord_guild.emojis:
            if emoji.name.casefold() == name.casefold():
                return str(emoji)
    return FALLBACK_EMOTE


async def _holder(delegate: Delegate | None, guild_tag: str) -> str:
    """How one contact slot prints — a mention and their Minecraft name.

    A slot whose holder no longer speaks for the guild prints as
    unclaimed. The reconcile vacates those rows outright, so this
    normally has nothing to catch; it covers the seconds between somebody
    changing sides and the pass that clears it, which is enough for the
    roster to never name somebody it's about to stop naming.
    """
    if delegate is None or not await contacts.represents(delegate, guild_tag):
        return UNCLAIMED
    username = await delegate_registry.display_name(delegate)
    return f"<@{delegate.discord_user_id}> (`{username}`)"


# --------------------------------------------------------------------------
# Syncing
# --------------------------------------------------------------------------


async def sync_channel(discord_guild: discord.Guild) -> RosterSync | None:
    """Make the Current Guilds channel say what the roster should say.

    Returns ``None`` when there's no channel configured or reachable —
    the roster is optional, and a bot that refused to start over an unset
    channel ID would be trading a feature for the whole service.
    """
    channel = _channel(discord_guild)
    if channel is None:
        return None

    chunks = await render_full_roster(discord_guild)
    try:
        history = [
            message
            async for message in channel.history(
                limit=HISTORY_LIMIT, oldest_first=True
            )
        ]
    except discord.HTTPException:
        logger.exception("roster: can't read the channel history; skipping this pass")
        return None

    rows = await RosterMessage.all().order_by("position")
    by_id = {message.id: message for message in history}
    order = {message.id: index for index, message in enumerate(history)}

    purged = await _purge_foreign(history, {row.discord_message_id for row in rows})
    resolved = [(row, by_id.get(row.discord_message_id)) for row in rows]
    cut = _first_gap(resolved, len(chunks), order)

    # Everything from the first gap down is re-sent rather than edited:
    # Discord can't insert a message above an existing one, so keeping the
    # order means the tail has to be rewritten in place order.
    deleted = 0
    for row, message in resolved[cut:]:
        if message is not None:
            deleted += await _delete(message)
        await row.delete()

    edited = 0
    for index in range(cut):
        row, message = resolved[index]
        edited += await _edit(message, chunks[index])
        row.position = index
        row.guild_tags = ",".join(chunks[index].tags)
        await row.save(update_fields=["position", "guild_tags", "updated_at"])

    created = 0
    for index in range(cut, len(chunks)):
        if await _send(channel, index, chunks[index]):
            created += 1
        else:
            # A send that failed leaves a hole, and everything after it
            # would land above nothing. Stop; the next pass starts here.
            break

    return RosterSync(
        guilds=sum(len(one.tags) for one in chunks),
        messages=len(chunks),
        created=created,
        edited=edited,
        deleted=deleted,
        purged=purged,
    )


def request_sync(discord_guild: discord.Guild) -> None:
    """Ask for a refresh shortly; asks inside the window collapse into one.

    A verification claims up to four contact slots and then sets a
    nickname, and each of those is a reason to redraw. Doing it once, a
    few seconds later, costs one channel read instead of five and lands
    after the whole join has settled rather than partway through it.

    Deliberately fire-and-forget: nothing a caller does should fail
    because the roster couldn't be redrawn, and the hourly pass is the
    backstop if this one is lost.
    """
    global _pending
    if _pending is not None and not _pending.done():
        return
    _pending = asyncio.create_task(_sync_soon(discord_guild))


async def wait_for_pending_sync() -> None:
    """Await a debounced sync if one is in flight. For tests, and shutdown."""
    task = _pending
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


async def _sync_soon(discord_guild: discord.Guild) -> None:
    await asyncio.sleep(SYNC_DELAY_SECONDS)
    try:
        summary = await sync_channel(discord_guild)
    except Exception:  # noqa: BLE001 — a background task with nobody to raise to
        logger.exception("roster: sync failed")
        return
    if summary is not None and summary.line():
        logger.info("roster: %s", summary.line())


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _channel(discord_guild: discord.Guild) -> discord.TextChannel | None:
    if not settings.roster_channel_id:
        logger.debug("roster: no channel configured; nothing to sync")
        return None
    channel = discord_guild.get_channel(settings.roster_channel_id)
    if channel is None:
        logger.warning(
            "roster: channel %s is not in the guild; skipping",
            settings.roster_channel_id,
        )
    return channel


def _first_gap(
    resolved: list[tuple[RosterMessage, discord.Message | None]],
    wanted: int,
    order: dict[int, int],
) -> int:
    """Where the on-screen sequence stops being usable as-is.

    Everything before this index is one of our messages, still present,
    and in the right order relative to its neighbours — so it can be
    edited in place. From here on, re-sending is the only way to get the
    order right.
    """
    last_seen = -1
    for index in range(wanted):
        if index >= len(resolved):
            return index
        _row, message = resolved[index]
        if message is None:
            return index  # deleted by hand, or lost to a failed send
        seen_at = order[message.id]
        if seen_at <= last_seen:
            # The channel disagrees with our recorded order. Trust the
            # channel — it's what readers see — and rebuild from here.
            return index
        last_seen = seen_at
    return wanted


async def _purge_foreign(
    history: list[discord.Message], tracked: set[int]
) -> int:
    """Delete anything in the channel that isn't a roster message of ours.

    The channel is the bot's alone by design, so anything else is either
    somebody talking in the wrong place or the residue of a pass that died
    between sending a message and recording it. Both want removing, and
    the second one would otherwise sit above the roster forever.
    """
    purged = 0
    for message in history:
        if message.id not in tracked:
            purged += await _delete(message)
    return purged


async def _delete(message: discord.Message) -> int:
    try:
        await message.delete()
    except discord.HTTPException:
        logger.exception("roster: couldn't delete message %s", message.id)
        return 0
    return 1


async def _edit(message: discord.Message, chunk_: RosterChunk) -> int:
    if message.content == chunk_.content:
        return 0  # already right — an hourly pass must be quiet
    try:
        await message.edit(content=chunk_.content, allowed_mentions=SILENT)
    except discord.HTTPException:
        logger.exception("roster: couldn't edit message %s", message.id)
        return 0
    return 1


async def _send(
    channel: discord.TextChannel, position: int, chunk_: RosterChunk
) -> bool:
    try:
        sent = await channel.send(chunk_.content, allowed_mentions=SILENT)
    except discord.HTTPException:
        logger.exception("roster: couldn't post roster message %d", position)
        return False
    await RosterMessage.create(
        position=position,
        discord_message_id=sent.id,
        guild_tags=",".join(chunk_.tags),
    )
    return True
