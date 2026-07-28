"""Keep the top guilds' banners in the server's custom-emote list.

Members can't upload emotes here, so the list is the bot's to manage and
**banners fill whatever the server isn't otherwise using**. The size of
that is the boost level: 50 slots at tier 0, 100 at 1, 150 at 2, 250 at
3. Nothing configures the count — :func:`budget` derives it from
`emoji_limit` minus the emotes a human uploaded minus
`ROSTER_EMOTE_RESERVE`, which is exactly what makes a boost gained or
lost take care of itself.

There are far more notable guilds than slots either way, so which ones
get a banner is decided by **how strongly each guild is notable**, not
by the roster's running order. A guild qualifying on four signals is
more securely part of the Hall than one scraping in on a single
leaderboard, so the placeholder falls to the guilds that qualify by the
least — see `notability.strength`. Guilds above the line are minted;
guilds that fall below it are evicted to make room, and **eviction runs
first** so the slots it frees are available to whatever displaced them.
A full list is a wall every later mint fails against silently.

Note this is deliberately a *different* order from the roster's. The
roster is sorted for a reader scanning it (guild level, highest first);
the slots go to whoever has most claim on one.

Two invariants, both the same shape as the guild-role rules in §11:

- **Only emotes we created are ever deleted.** `GuildEmote` records the
  ones we minted. An emote that happens to be named after a guild might
  be somebody's own from years ago, and deleting it breaks every message
  that used it, irreversibly.
- **An unchanged banner is never re-uploaded.** Every upload is a *new*
  emote ID, so re-minting silently breaks every message and role icon
  already pointing at the old one. The rendered PNG's hash is what
  decides, so a re-render that produces identical bytes costs nothing.

The rendered PNG has **two** consumers, and this module drives both from
one render: the custom emote, and the guild role's `display_icon`
(§11 — an emote cannot live in a role *name*, so the icon is the whole
mechanism). Rendering separately for each would double the work per
guild per hour for no gain.

Upstream work is bounded in two ways, because Wynnpool rate-limits its
guild endpoint at around a dozen requests and a full roster is fifty
guilds. **An unchanged banner is never re-fetched** — a guild whose emote
is up and was checked inside `RECHECK_AFTER` costs nothing at all, so a
settled server makes no requests — and a pass fetches at most
`FETCHES_PER_PASS` of whatever is left, reporting how many are still
outstanding. One guild's failure never costs the rest, and a 429 stops
the pass rather than collecting forty more of them.

Guilds that miss out wear a shared blank banner, and the bot **mints
that itself** on the first pass — rendered through the same pipeline, so
it matches the real ones in size and proportion, and from no layers at
all, so the fallback needs nothing from Wynnpool to exist. It is found by
*name*: an operator who already made one keeps theirs, and it is never
deleted, having no guild to fall out of the budget with.

The roster picks all of this up on its own: `roster.emote_for` tries our
banner for the guild, then the placeholder, then a plain unicode flag.
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
import httpx

from hall_monitor.config import settings
from hall_monitor.db.models import GuildEmote, GuildRole

# The shared blank banner every guild outside the budget wears, named
# for Wynncraft's reserved `NONE` guild ("Nobody"). That name is the
# point: `NONE` is reserved by the server and no real guild can ever
# hold it, so the placeholder slots into the same tag-named scheme as
# every other banner with no chance of collision — where a name like
# `Empty_Banner` was only ever unique by convention.
#
# Held by name rather than by a recorded ID, which is what lets the bot
# adopt one an operator made by hand instead of uploading a second.
PLACEHOLDER_NAME = "NONE"

# What the placeholder used to be called. Renamed in place rather than
# re-uploaded, because a new emote is a new ID and every message already
# carrying the old one would break.
_FORMER_PLACEHOLDER_NAMES = ("Empty_Banner",)

# Wynnpool's guild endpoint rate-limits at roughly a dozen requests
# before it starts answering 429, and a full roster is fifty guilds. So a
# pass fetches at most this many banners and leaves the rest for the next
# one — the hourly job fills a fresh server in over a few hours, and
# steady state is zero fetches because an unchanged banner is never
# re-fetched at all.
FETCHES_PER_PASS = 10

# How long a banner is trusted before it's fetched again.
#
# Guilds redesign roughly twice a year, so *any* polling window is mostly
# waste: at a week it's ~2 500 requests a year to catch ~100 real
# changes, and shortening it to two days triples that without helping the
# person who noticed today. So the window stays polite and
# `refresh_guild` covers the case somebody actually spots — which, for an
# event this rare, is the path that matters.
RECHECK_AFTER = timedelta(days=7)
from hall_monitor.services import (
    banner_render,
    guild_roles,
    guild_tag as tags,
    notability,
    roster,
)
from hall_monitor.external import wynnpool

logger = logging.getLogger(__name__)


class Reconciled:
    """What one pass over the emote list did."""

    __slots__ = (
        "minted", "refreshed", "evicted", "failed", "icons", "placeholder",
        "pending", "budget", "wanted",
    )

    def __init__(self) -> None:
        self.minted = self.refreshed = self.evicted = self.failed = 0
        self.icons = 0
        self.placeholder = False
        self.pending = 0
        self.budget = 0
        self.wanted = 0

    def line(self) -> str:
        work = ", ".join(
            f"{count} {label}"
            for label, count in (
                ("minted", self.minted),
                ("refreshed", self.refreshed),
                ("evicted", self.evicted),
                ("role icons set", self.icons),
                ("blank banner minted", int(self.placeholder)),
                ("failed", self.failed),
            )
            if count
        )
        # Saying how many are left is the difference between "it worked"
        # and "it worked so far" — a pass that stopped at its fetch cap
        # looks identical to a finished one otherwise.
        tail = (
            f"; {self.pending} still to fetch (next pass, or run it again)"
            if self.pending
            else ""
        )
        return (
            f"{self.wanted} guild(s) inside a budget of {self.budget}; "
            f"{work or 'nothing to do'}{tail}"
        )


async def reconcile(discord_guild: discord.Guild) -> Reconciled:
    """Mint banners for the top guilds and evict the ones that dropped off.

    Self-correcting against the boost level: :func:`budget` reads the
    server's current slot count every pass, so a level gained means more
    banners next time and a level lost means the tail is evicted to fit.
    A `GUILD_UPDATE` listener runs this immediately on a boost change so
    the wait isn't an hour, but the hourly pass would get there anyway.
    """
    summary = Reconciled()
    summary.budget = await budget(discord_guild)

    wanted = [one.tag for one in await by_strength()][: summary.budget]
    summary.wanted = len(wanted)

    # Eviction first, so the slots it frees are available to whatever
    # displaced them — and so the placeholder has somewhere to go on a
    # server whose list is already full of last hour's banners.
    await _evict_all_but(discord_guild, wanted, summary)
    summary.placeholder = await ensure_placeholder(discord_guild)

    # Losing role icons needs no banner bytes, so it can't wait on the
    # per-guild loop below — that loop skips any guild whose banner is
    # already current, which on a settled server is all of them.
    if not guild_roles.has_role_icons(discord_guild):
        await guild_roles.forget_role_icons()

    fetches = FETCHES_PER_PASS
    for tag in wanted:
        try:
            fetches -= await _ensure(
                discord_guild, tag, summary, may_fetch=fetches > 0
            )
        except httpx.HTTPStatusError as exc:
            summary.failed += 1
            if exc.response.status_code == 429:
                # The bucket is already paused; grinding through forty
                # more guilds would just collect forty more 429s. Stop
                # fetching and let the next pass carry on.
                logger.warning(
                    "emotes: Wynnpool is rate-limiting; stopping after %s and "
                    "picking up where we left off next pass",
                    tag,
                )
                fetches = 0
            else:
                logger.exception("emotes: %s failed", tag)
        except Exception:  # noqa: BLE001 — one guild must not cost the rest
            summary.failed += 1
            logger.exception("emotes: %s failed", tag)
    return summary


async def ensure_placeholder(discord_guild: discord.Guild) -> bool:
    """Make sure the shared blank banner exists. Returns whether we made it.

    Found by name, so a `NONE` emote somebody uploaded by hand is
    adopted rather than duplicated, and an older `Empty_Banner` of ours
    is renamed in place rather than replaced — and, unlike the per-guild banners,
    never deleted: it belongs to no guild, so nothing can evict it, and
    the roster's whole fallback chain rests on it.

    A failure here is not worth a fuss. The roster falls back to a plain
    unicode flag, which is exactly what it did before this existed.
    """
    if not settings.roster_emotes_enabled:
        return False
    if _placeholder(discord_guild) is not None:
        return False
    if await _rename_former_placeholder(discord_guild):
        return False
    try:
        png = await banner_render.render_placeholder()
        emoji = await discord_guild.create_custom_emoji(
            name=PLACEHOLDER_NAME,
            image=png,
            reason="hall-monitor: blank banner for guilds outside the emote budget",
        )
    except discord.HTTPException:
        logger.warning(
            "emotes: couldn't upload the %s placeholder; the roster will use a "
            "plain flag instead",
            PLACEHOLDER_NAME,
            exc_info=True,
        )
        return False
    logger.info("emotes: uploaded the blank banner as :%s:", emoji.name)
    return True


def _placeholder(discord_guild: discord.Guild) -> discord.Emoji | None:
    return _named(discord_guild, PLACEHOLDER_NAME)


def _named(discord_guild: discord.Guild, name: str) -> discord.Emoji | None:
    for emoji in discord_guild.emojis:
        if emoji.name.casefold() == name.casefold():
            return emoji
    return None


async def _rename_former_placeholder(discord_guild: discord.Guild) -> bool:
    """Carry an older placeholder over to the current name, in place.

    Renaming keeps the emote's ID, so every message already using it
    still renders. Uploading a fresh one and deleting the old would break
    all of them and spend a slot doing it.
    """
    for name in _FORMER_PLACEHOLDER_NAMES:
        emoji = _named(discord_guild, name)
        if emoji is None:
            continue
        try:
            await emoji.edit(
                name=PLACEHOLDER_NAME,
                reason=f"hall-monitor: {name} is now {PLACEHOLDER_NAME}",
            )
        except discord.HTTPException:
            logger.warning(
                "emotes: couldn't rename :%s: to :%s:", name, PLACEHOLDER_NAME,
                exc_info=True,
            )
            return False
        logger.info("emotes: renamed :%s: to :%s:", name, PLACEHOLDER_NAME)
        return True
    return False


async def refresh_guild(
    discord_guild: discord.Guild, guild_tag: str
) -> Reconciled:
    """Re-fetch one guild's banner now, whatever the re-check window says.

    The answer to "they changed their banner this morning". Redesigns are
    rare enough — about twice a year per guild — that polling for them
    frequently would be thousands of requests a year to catch a hundred
    events, so the automatic window stays slow and this covers the case
    where a human has already noticed and wants it fixed.

    Goes through the same `_ensure` the sweep uses rather than a second
    path that could drift from it.
    """
    summary = Reconciled()
    summary.budget = await budget(discord_guild)
    summary.wanted = 1
    await _ensure(discord_guild, guild_tag, summary, force=True)
    return summary


async def holds_emote(discord_guild: discord.Guild, guild_tag: str) -> bool:
    """Whether this guild currently has a banner of ours in the server."""
    row = await GuildEmote.filter(guild_tag__iexact=guild_tag).first()
    return _find(discord_guild, row) is not None


async def rendered_banner(guild_tag: str, guild_name: str | None) -> bytes | None:
    """The PNG for a guild, or ``None`` if nothing upstream knows its banner.

    Addressed by *name*, because that's the only key Wynnpool's guild
    endpoint takes — so a tag with no name resolved (see
    `notability._learn_missing_names`) has nothing to ask for.
    """
    if not guild_name:
        logger.info("emotes: no guild name known for %s; can't fetch a banner", guild_tag)
        return None
    details = await wynnpool.guild_details(guild_name)
    if details is None or details.banner is None:
        logger.info("emotes: wynnpool has no banner for %s", guild_name)
        return None
    return await banner_render.render_banner(details.banner)


async def by_strength() -> list[roster.ListedGuild]:
    """Every listed guild, most strongly notable first.

    The order slots are handed out in, and not the same as the roster's:
    that one is sorted for someone reading down it, this one for who has
    most claim on a scarce emote. `notability.strength` counts matched
    signals first and breaks ties on the numbers behind them.

    A guild with no cached measurement sorts last — that's a fresh force
    override, and we know nothing about it that would justify putting it
    ahead of a guild we've actually measured. The tag is the final
    tiebreak, purely so the order is stable between passes and a
    coin-flip doesn't churn an emote every hour.
    """
    strengths = await notability.strength_by_tag()
    listed = await roster.listed_guilds()
    return sorted(
        listed,
        key=lambda one: (
            [-value for value in strengths.get(tags.normalise(one.tag), ())] or [1],
            one.tag.upper(),
        ),
    )


async def budget(discord_guild: discord.Guild) -> int:
    """How many banners fit right now: the slots nobody else is using.

    Derived, not configured, and that's what makes boost changes take
    care of themselves. `emoji_limit` *is* the boost level — 50 slots at
    tier 0, 100 at 1, 150 at 2, 250 at 3 — so a server that gains a level
    simply has a bigger number here on the next pass, and one that loses
    a level has a smaller one and evicts down to fit.

    Two subtractions:

    - **Emotes we didn't upload.** Members can't add them, but admins
      can, and those slots are genuinely taken. Counting them keeps the
      budget honest instead of minting into space that isn't there and
      failing on the last few.
    - **The reserve.** Filling the list to the brim means an admin
      wanting to add one has to delete a banner first — which the next
      pass would put straight back. A slot or two of headroom avoids
      that fight entirely.

    Animated emotes are ignored: Discord counts them against a separate
    pool of the same size, and our banners are static.

    The blank-banner placeholder costs a slot too. Once it exists it's
    counted with the emotes we didn't upload, and before it exists a slot
    is held for it — otherwise the last guild in the budget would be
    minted into the space the placeholder is about to need, and fail.
    """
    if not settings.roster_emotes_enabled:
        return 0
    limit = getattr(discord_guild, "emoji_limit", 0)
    ours = {
        row["discord_emoji_id"]
        for row in await GuildEmote.all().values("discord_emoji_id")
    }
    foreign = sum(
        1
        for emoji in discord_guild.emojis
        if not getattr(emoji, "animated", False) and emoji.id not in ours
    )
    held_back = max(0, settings.roster_emote_reserve)
    if _placeholder(discord_guild) is None:
        held_back += 1
    return max(0, limit - foreign - held_back)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


async def _ensure(
    discord_guild: discord.Guild,
    guild_tag: str,
    summary: Reconciled,
    *,
    may_fetch: bool = True,
    force: bool = False,
) -> int:
    """Settle one guild's banner. Returns how many upstream fetches it cost.

    Returning the cost is what lets :func:`reconcile` bound a pass: a
    guild whose banner is already up and recently checked costs nothing,
    so a settled server does no upstream work at all and only a genuinely
    new or stale one spends from the budget.
    """
    existing = await GuildEmote.filter(guild_tag__iexact=guild_tag).first()
    emoji = _find(discord_guild, existing)
    if existing is not None and emoji is None:
        # Deleted by hand at some point. Stop claiming it and re-mint.
        await existing.delete()
        existing = None

    if (
        not force
        and existing is not None
        and emoji is not None
        and not _is_stale(existing)
        and not await _icon_out_of_sync(discord_guild, guild_tag, existing)
    ):
        return 0  # already up and recently checked — nothing to ask anyone

    if not may_fetch:
        # Out of fetches for this pass. Counted rather than dropped, so
        # the reply says how much is left instead of implying it's done.
        summary.pending += 1
        return 0

    name = await _guild_name(guild_tag)
    png = await rendered_banner(guild_tag, name)
    if png is None:
        # Nothing upstream knows this guild's banner. Keep whatever is
        # already there — a miss isn't a reason to lose a good emote — but
        # mark it checked so the next pass spends its budget elsewhere.
        await _mark_checked(existing)
        return 1
    digest = banner_render.image_hash(png)
    await _push_role_icon(discord_guild, guild_tag, png, summary)

    if existing is not None and emoji is not None:
        await _mark_checked(existing)
        if existing.image_hash == digest:
            return 1  # unchanged — an hourly pass must be quiet
        # The banner really changed. Discord has no "replace an emote's
        # image", so this is a delete and a re-upload, and the ID moves.
        if not await _delete(emoji, guild_tag):
            summary.failed += 1
            return 1
        await existing.delete()
        summary.refreshed += 1
        await _upload(discord_guild, guild_tag, png, digest, summary, minted=False)
        return 1

    await _upload(discord_guild, guild_tag, png, digest, summary, minted=True)
    return 1


async def _icon_out_of_sync(
    discord_guild: discord.Guild, guild_tag: str, row: GuildEmote
) -> bool:
    """Whether the role's icon needs the banner bytes we'd otherwise skip.

    Skipping the fetch for an unchanged banner is what keeps a settled
    server quiet, but the role icon rides on those same bytes — so a
    server that has just regained boost level 2 would sit iconless until
    the weekly re-check came round. The two hashes disagreeing is exactly
    that situation, and it resolves itself in one pass.

    False when the server can't have role icons at all, or the role isn't
    one of ours: below boost level 2 the recorded hash is deliberately
    cleared (`guild_roles.sync_role_icon`), and treating that as
    out-of-sync would have an unboosted server re-fetching every banner
    it has, every hour, forever.
    """
    if not guild_roles.has_role_icons(discord_guild):
        return False
    role = await guild_roles.resolve_role(discord_guild, guild_tag)
    if role is None:
        return False
    owned = await GuildRole.filter(
        guild_tag__iexact=guild_tag, discord_role_id=role.id
    ).first()
    return owned is not None and owned.icon_hash != row.image_hash


def _is_stale(row: GuildEmote) -> bool:
    """Whether a banner is due another look."""
    if row.checked_at is None:
        return True  # minted before this was recorded
    return datetime.now(timezone.utc) - row.checked_at > RECHECK_AFTER


async def _mark_checked(row: GuildEmote | None) -> None:
    if row is None:
        return
    row.checked_at = datetime.now(timezone.utc)
    await row.save(update_fields=["checked_at"])


async def _upload(
    discord_guild: discord.Guild,
    guild_tag: str,
    png: bytes,
    digest: str,
    summary: Reconciled,
    *,
    minted: bool,
) -> discord.Emoji | None:
    try:
        emoji = await discord_guild.create_custom_emoji(
            name=_emote_name(guild_tag),
            image=png,
            reason=f"hall-monitor: {guild_tag} banner",
        )
    except discord.HTTPException:
        logger.exception(
            "emotes: couldn't upload the %s banner (slots full, or no "
            "Manage Expressions)",
            guild_tag,
        )
        summary.failed += 1
        return None
    await GuildEmote.update_or_create(
        guild_tag=guild_tag,
        defaults={
            "discord_emoji_id": emoji.id,
            "image_hash": digest,
            # Uploading *is* a check. Without this a freshly minted
            # banner reads as never-checked and gets re-fetched on every
            # pass — which is most of what the skip exists to avoid.
            "checked_at": datetime.now(timezone.utc),
        },
    )
    if minted:
        summary.minted += 1
    logger.info("emotes: uploaded the %s banner as :%s:", guild_tag, emoji.name)
    return emoji


async def _evict_all_but(
    discord_guild: discord.Guild, wanted: list[str], summary: Reconciled
) -> None:
    """Drop our emotes for guilds that have fallen outside the budget.

    Runs *before* minting so the freed slots are available to the guilds
    that displaced them — otherwise a full list makes every mint fail and
    the boundary never moves.
    """
    keep = {tags.normalise(tag) for tag in wanted}
    for row in await GuildEmote.all():
        if tags.normalise(row.guild_tag) in keep:
            continue
        emoji = _find(discord_guild, row)
        if emoji is not None and not await _delete(emoji, row.guild_tag):
            summary.failed += 1
            continue
        await row.delete()
        # The role keeps its colour but loses the banner: an icon left
        # behind would outlive the emote it was rendered from, and drift
        # the moment the guild changes its banner.
        await _push_role_icon(discord_guild, row.guild_tag, None, summary)
        summary.evicted += 1


async def _push_role_icon(
    discord_guild: discord.Guild,
    guild_tag: str,
    png: bytes | None,
    summary: Reconciled,
) -> None:
    """Second consumer of the same bytes — the role's display icon."""
    role = await guild_roles.resolve_role(discord_guild, guild_tag)
    if role is None:
        return
    if await guild_roles.sync_role_icon(discord_guild, role, guild_tag, icon=png):
        summary.icons += 1


def _find(
    discord_guild: discord.Guild, row: GuildEmote | None
) -> discord.Emoji | None:
    """Our emote for a row, by recorded ID only.

    Never by name — a name match would let the bot delete an emote the
    server made itself, which is the one mistake here that can't be
    undone.
    """
    if row is None:
        return None
    return discord.utils.get(discord_guild.emojis, id=row.discord_emoji_id)


async def _delete(emoji: discord.Emoji, guild_tag: str) -> bool:
    try:
        await emoji.delete(reason=f"hall-monitor: {guild_tag} banner recycled")
    except discord.HTTPException:
        logger.exception("emotes: couldn't delete the %s banner emote", guild_tag)
        return False
    return True


def _emote_name(guild_tag: str) -> str:
    """Discord allows 2–32 characters of word characters only.

    Guild tags are three or four alphanumerics in practice, but the API
    permits spaces and underscores (`services/guild_tag.py`), so anything
    outside the allowed set becomes an underscore rather than a 400.
    """
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in guild_tag)
    return (cleaned or "guild")[:32].ljust(2, "_")


async def _guild_name(guild_tag: str) -> str | None:
    for one in await roster.listed_guilds():
        if tags.matches(one.tag, guild_tag):
            # `name` falls back to the tag when nothing is known, and the
            # tag is not a key Wynnpool's guild endpoint accepts.
            return None if tags.matches(one.name, guild_tag) else one.name
    return None
