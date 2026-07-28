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

The roster picks emotes up on its own: `roster.emote_for` looks for an
emote named after the guild tag and falls back to the shared placeholder,
so this module never has to tell it anything.
"""

import logging

import discord

from hall_monitor.config import settings
from hall_monitor.db.models import GuildEmote
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
        "minted", "refreshed", "evicted", "failed", "icons", "budget", "wanted"
    )

    def __init__(self) -> None:
        self.minted = self.refreshed = self.evicted = self.failed = 0
        self.icons = 0
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
                ("failed", self.failed),
            )
            if count
        )
        return (
            f"{self.wanted} guild(s) inside a budget of {self.budget}; "
            f"{work or 'nothing to do'}"
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

    await _evict_all_but(discord_guild, wanted, summary)
    for tag in wanted:
        await _ensure(discord_guild, tag, summary)
    return summary


async def ensure_emote_for_guild(
    discord_guild: discord.Guild, guild_tag: str
) -> discord.Emoji | None:
    """Mint or refresh one guild's banner emote. ``None`` if it couldn't be.

    Public because `~script render_banner` and a future manual override
    want the same path the reconcile takes, rather than a second one that
    can drift from it.
    """
    summary = Reconciled()
    return await _ensure(discord_guild, guild_tag, summary)


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
    return max(0, limit - foreign - max(0, settings.roster_emote_reserve))


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


async def _ensure(
    discord_guild: discord.Guild, guild_tag: str, summary: Reconciled
) -> discord.Emoji | None:
    existing = await GuildEmote.filter(guild_tag__iexact=guild_tag).first()
    emoji = _find(discord_guild, existing)
    if existing is not None and emoji is None:
        # Deleted by hand at some point. Stop claiming it and re-mint.
        await existing.delete()
        existing = None

    name = await _guild_name(guild_tag)
    png = await rendered_banner(guild_tag, name)
    if png is None:
        return emoji  # keep whatever is already there; a miss isn't a reason to lose it
    digest = banner_render.image_hash(png)
    await _push_role_icon(discord_guild, guild_tag, png, summary)

    if existing is not None and emoji is not None:
        if existing.image_hash == digest:
            return emoji  # unchanged — an hourly pass must be quiet
        # The banner really changed. Discord has no "replace an emote's
        # image", so this is a delete and a re-upload, and the ID moves.
        if not await _delete(emoji, guild_tag):
            summary.failed += 1
            return emoji
        await existing.delete()
        summary.refreshed += 1
        return await _upload(discord_guild, guild_tag, png, digest, summary, minted=False)

    return await _upload(discord_guild, guild_tag, png, digest, summary, minted=True)


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
        defaults={"discord_emoji_id": emoji.id, "image_hash": digest},
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
