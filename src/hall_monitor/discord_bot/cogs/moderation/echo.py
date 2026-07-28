"""``~echo`` — say something as the bot, without the command showing.

A janitor writes an announcement, the invoking message disappears, and
what's left is the bot having said it. Useful because the Hall is a room
of representatives from rival guilds: an announcement in a person's name
carries their guild's weight with it, and often shouldn't.

**Two commands, not one with a flag.** ``~silent_echo`` (janitor)
notifies nobody; ``~noisy_echo`` (monitor) may ping anything including
``@everyone``. Both render mentions identically — the difference is only
whether anybody's client rings. That split exists because the two are
wanted in genuinely different moments and a single command has to guess
wrong half the time, and the tiers follow the cost: waking three hundred
people is a monitor's call. ``~echo`` aliases the silent one, so the
short name anybody reaches for first is the one that can't wake the room.

**Attachments come with it.** An announcement that loses its image is
worse than no echo, and re-uploading is the only way — Discord gives no
way to move an attachment between messages. They're re-fetched and sent
as fresh files, which spends the bandwidth twice and is worth it.
"""

import logging

import discord
from discord.ext import commands

from hall_monitor.discord_bot.cogs.moderation import _speak
from hall_monitor.discord_bot.permissions import is_janitor, is_monitor

logger = logging.getLogger(__name__)


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


async def speak(
    ctx: commands.Context, content: str, *, mentions: discord.AllowedMentions
) -> None:
    if ctx.guild is None:
        await ctx.reply("run this in the server.")
        return
    files = await collect_files(ctx.message)
    if not content and not files:
        await ctx.reply("give me something to say — text, an image, or both.")
        return
    await _speak.post_then_delete(
        ctx, content=content, files=files, mentions=mentions, label="echo"
    )


class Echo(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="silent_echo", aliases=["echo"])
    @is_janitor()
    async def silent_echo(self, ctx: commands.Context, *, content: str = "") -> None:
        """say something as the bot, pinging nobody"""
        await speak(ctx, content, mentions=_speak.SILENT)

    @commands.command(name="noisy_echo")
    @is_monitor()
    async def noisy_echo(self, ctx: commands.Context, *, content: str = "") -> None:
        """say something as the bot, and let the mentions ring"""
        await speak(ctx, content, mentions=_speak.NOISY)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Echo(bot))
