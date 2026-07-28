"""``~embed`` — post a formatted panel as the bot.

``~echo``'s sibling, for the things that want to look like a notice
rather than a message: a rules panel, a "how to verify" block, an event
announcement. Same deletion of the invoking message, for the same
reasons, and the same silent/noisy split — ``~silent_embed`` (janitor)
and ``~noisy_embed`` (monitor), with ``~embed`` aliasing the silent one.

**Mentions inside an embed never notify anybody.** That's Discord, not a
choice here: notifications are raised from a message's *content*, and an
embed's body is not content — a `@here` in a description renders as
`@here` and rings for nobody, whatever `allowed_mentions` says. So a
`~noisy_embed` that only relaxed the mention rules would be a command
claiming to ping and silently not doing it, which is the exact failure
shape §12.3 is a list of.

Hence **`ping=`**: its text is sent as the message content *above* the
embed, where notifications actually come from.

    ~noisy_embed ping="@here" title="Maintenance" desc="Back in an hour."

`~silent_embed` refuses `ping=` outright rather than dropping it, so
nobody walks away believing they notified the room.

**One-shot syntax rather than an interactive prompt**, which the plan
left open. A prompt means conversational state per user across messages,
and every way out of it — they wander off, they answer with a different
command, the bot restarts halfway — is a state somebody has to handle.
The command is one line either way, and a line can be edited and re-run
when it comes out wrong, which a half-finished prompt can't.

Keys are ``title``, ``desc``/``description``, ``colour``/``color``,
``url``, ``image``, ``footer``, and ``ping`` on the noisy one. Values
take double quotes when they contain spaces. Anything left over becomes
the description, so ``~embed Just a sentence`` does the obvious thing —
somebody's first use will not have the syntax in front of them.
"""

import logging
import re

import discord
from discord.ext import commands

from hall_monitor.discord_bot.cogs.moderation import _speak
from hall_monitor.discord_bot.permissions import is_janitor, is_monitor

logger = logging.getLogger(__name__)

# `key="quoted value"` or `key=bare`. DOTALL so a quoted value can span
# lines — an embed description is the one place that's normal.
_FIELD = re.compile(r'(\w+)=(?:"(.*?)"|(\S+))', re.DOTALL)

ALIASES = {"description": "desc", "color": "colour"}
KNOWN = {"title", "desc", "colour", "url", "image", "footer", "ping"}

DEFAULT_COLOUR = discord.Colour.blurple()


class BadEmbed(Exception):
    """Carries the user-facing reason an embed can't be built."""


def parse_colour(raw: str) -> discord.Colour:
    """``#5865F2``, ``5865F2``, or a Discord colour name like ``blurple``."""
    try:
        return discord.Colour(int(raw.strip().lstrip("#"), 16))
    except ValueError:
        pass
    factory = getattr(discord.Colour, raw.strip().lower(), None)
    if callable(factory):
        try:
            named = factory()
        except TypeError:
            named = None
        if isinstance(named, discord.Colour):
            return named
    raise BadEmbed(
        f"`{raw}` isn't a colour I know — try a hex value like `#5865F2`, or a "
        "name like `blurple`, `red`, `green`."
    )


def parse(content: str, *, allow_ping: bool = False) -> tuple[discord.Embed, str]:
    """Build an embed from a ``key=value`` line, plus the content above it.

    Returns ``(embed, ping_text)``. ``ping_text`` is empty unless
    ``ping=`` was given on a command that allows it.
    """
    fields: dict[str, str] = {}
    leftover = content
    for match in _FIELD.finditer(content):
        key = ALIASES.get(match.group(1).lower(), match.group(1).lower())
        if key not in KNOWN:
            continue  # not one of ours — leave it in the prose
        fields[key] = match.group(2) if match.group(2) is not None else match.group(3)
        leftover = leftover.replace(match.group(0), "", 1)

    ping = fields.pop("ping", "")
    if ping and not allow_ping:
        # Refused rather than dropped: silently ignoring it would leave a
        # janitor believing they'd notified the room.
        raise BadEmbed(
            "`ping=` only works on `~noisy_embed`, which is monitor-only — a "
            "silent embed notifies nobody by design. Ask a monitor, or drop "
            "the `ping=`."
        )

    # Bare text becomes the description, so a first attempt made without
    # the syntax still produces something rather than a complaint.
    description = fields.get("desc") or leftover.strip()
    if not description and not fields.get("title") and not fields.get("image"):
        raise BadEmbed(
            'give me something to show — e.g. `~embed title="Notice" '
            'desc="what it says"`.'
        )

    embed = discord.Embed(
        title=fields.get("title"),
        description=description or None,
        colour=parse_colour(fields["colour"]) if "colour" in fields else DEFAULT_COLOUR,
        url=fields.get("url"),
    )
    if image := fields.get("image"):
        embed.set_image(url=image)
    if footer := fields.get("footer"):
        embed.set_footer(text=footer)
    return embed, ping


async def post(
    ctx: commands.Context,
    content: str,
    *,
    mentions: discord.AllowedMentions,
    allow_ping: bool,
) -> None:
    if ctx.guild is None:
        await ctx.reply("run this in the server.")
        return
    try:
        built, ping = parse(content, allow_ping=allow_ping)
    except BadEmbed as exc:
        await ctx.reply(str(exc))
        return
    await _speak.post_then_delete(
        ctx, content=ping, embed=built, mentions=mentions, label="embed"
    )


class Embed(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="silent_embed", aliases=["embed"])
    @is_janitor()
    async def silent_embed(self, ctx: commands.Context, *, content: str = "") -> None:
        """post a formatted panel as the bot, pinging nobody"""
        await post(ctx, content, mentions=_speak.SILENT, allow_ping=False)

    @commands.command(name="noisy_embed")
    @is_monitor()
    async def noisy_embed(self, ctx: commands.Context, *, content: str = "") -> None:
        """post a panel and ping with `ping="@here"` above it"""
        await post(ctx, content, mentions=_speak.NOISY, allow_ping=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Embed(bot))
