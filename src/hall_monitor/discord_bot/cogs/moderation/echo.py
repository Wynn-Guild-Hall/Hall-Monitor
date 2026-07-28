"""``~echo`` — say something as the bot, without the command showing.

A janitor writes an announcement, the invoking message disappears, and
what's left is the bot having said it. Useful because the Hall is a room
of representatives from rival guilds: an announcement in a person's name
carries their guild's weight with it, and often shouldn't.

Two decisions worth stating.

**Attachments come with it.** An announcement that loses its image is
worse than no echo, and re-uploading is the only way — Discord gives no
way to move an attachment between messages. They're re-fetched and sent
as fresh files, which spends the bandwidth twice and is worth it.

**It cannot ping the server.** Users and roles mention normally, so
``@Guild Hall Delegate`` works and reads as intended, but ``@everyone``
and ``@here`` are inert. This bot has exactly one deliberate ping
(DESIGN.md §16.4), gated on three guilds agreeing, and a command that
quietly launders one past that would make the rule meaningless. A
janitor who genuinely wants to notify the server can post it under their
own name, where at least it's attributable.
"""

import logging

import discord
from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_janitor

logger = logging.getLogger(__name__)

# Users and roles yes; `@everyone`/`@here` no. See the module docstring.
ECHO_MENTIONS = discord.AllowedMentions(everyone=False, users=True, roles=True)


async def collect_files(message: discord.Message) -> list[discord.File]:
    """Re-download a message's attachments so they can be re-sent.

    One that fails is skipped rather than costing the whole echo: a
    missing image is a visible, fixable problem, and a swallowed
    announcement is not.
    """
    files = []
    for attachment in message.attachments:
        try:
            files.append(await attachment.to_file())
        except discord.HTTPException:
            logger.exception(
                "echo: couldn't re-upload attachment %s", attachment.filename
            )
    return files


class Echo(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="echo")
    @is_janitor()
    async def echo(self, ctx: commands.Context, *, content: str = "") -> None:
        """say something as the bot, hiding who asked"""
        if ctx.guild is None:
            await ctx.reply("run this in the server.")
            return
        files = await collect_files(ctx.message)
        if not content and not files:
            await ctx.reply("give me something to say — text, an image, or both.")
            return

        try:
            await ctx.send(content or "", files=files, allowed_mentions=ECHO_MENTIONS)
        except discord.HTTPException:
            logger.exception("echo: couldn't post for %s", ctx.author.id)
            await ctx.reply("that broke on my end — the details are in my logs.")
            return

        # Deleted last, and only once the echo is actually out. The other
        # order loses the message entirely when the send fails, and what
        # it loses is something a person has just written.
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            logger.warning(
                "echo: posted for %s but couldn't delete their message "
                "(needs Manage Messages here)",
                ctx.author.id,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Echo(bot))
