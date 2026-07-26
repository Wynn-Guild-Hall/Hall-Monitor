"""``~echo`` — re-post the invoking message verbatim as the bot."""

from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_janitor


class Echo(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="echo")
    @is_janitor()
    async def echo(self, ctx: commands.Context, *, content: str = "") -> None:
        raise NotImplementedError


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Echo(bot))
