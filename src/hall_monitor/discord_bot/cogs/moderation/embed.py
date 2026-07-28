"""``~embed`` — post a formatted panel as the bot.

``~echo``'s sibling, for things that want to look like a notice rather
than a message: a rules panel, a "how to verify" block, an event
announcement. Same deletion of the invoking message, for the same
reasons.

**One-shot syntax rather than an interactive prompt**, which the plan
left open. A prompt means holding conversational state per user across
messages, and every way out of it — they wander off, they answer with a
different command, the bot restarts halfway — is a state somebody has to
handle. The whole command is one line either way, and a line can be
edited and re-run when it comes out wrong, which a half-finished prompt
can't:

    ~embed title="Verification" desc="Head to hall.wynnvets.org/join" colour=#5865F2

Keys are ``title``, ``desc``/``description``, ``colour``/``color``,
``url``, ``image``, ``footer``. Values take double quotes when they
contain spaces. Anything left over after the recognised keys becomes the
description, so ``~embed Just a sentence`` does the obvious thing —
somebody's first use of this will not have the syntax in front of them.

Newlines survive: Discord sends a multi-line message as one string, so a
quoted value spans lines exactly as typed.
"""

import logging
import re

import discord
from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_janitor

logger = logging.getLogger(__name__)

# `key="quoted value"` or `key=bare`. DOTALL so a quoted value can span
# lines — an embed description is the one place that's normal.
_FIELD = re.compile(r'(\w+)=(?:"(.*?)"|(\S+))', re.DOTALL)

ALIASES = {"description": "desc", "color": "colour"}
KNOWN = {"title", "desc", "colour", "url", "image", "footer"}

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


def parse(content: str) -> discord.Embed:
    """Build an embed from a ``key=value`` line. Raises :class:`BadEmbed`."""
    fields: dict[str, str] = {}
    leftover = content
    for match in _FIELD.finditer(content):
        key = ALIASES.get(match.group(1).lower(), match.group(1).lower())
        if key not in KNOWN:
            continue  # not one of ours — leave it in the prose
        fields[key] = match.group(2) if match.group(2) is not None else match.group(3)
        leftover = leftover.replace(match.group(0), "", 1)

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
    return embed


class Embed(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="embed")
    @is_janitor()
    async def embed(self, ctx: commands.Context, *, content: str = "") -> None:
        """post a formatted panel as the bot"""
        if ctx.guild is None:
            await ctx.reply("run this in the server.")
            return
        try:
            built = parse(content)
        except BadEmbed as exc:
            await ctx.reply(str(exc))
            return

        try:
            await ctx.send(embed=built)
        except discord.HTTPException as exc:
            logger.exception("embed: couldn't post for %s", ctx.author.id)
            # Discord validates embeds server-side — a malformed `url`, an
            # over-long field — and its complaint is far more use to the
            # author than "that broke on my end" would be.
            await ctx.reply(f"Discord refused that embed: {exc.text or exc}")
            return

        try:
            await ctx.message.delete()
        except discord.HTTPException:
            logger.warning(
                "embed: posted for %s but couldn't delete their message "
                "(needs Manage Messages here)",
                ctx.author.id,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Embed(bot))
