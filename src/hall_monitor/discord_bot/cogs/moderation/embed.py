"""``~embed`` — minimal constructor for a Discord embed message."""

from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_janitor


class Embed(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="embed", hidden=True)
    @is_janitor()
    async def embed(self, ctx: commands.Context, *, content: str = "") -> None:
        raise NotImplementedError


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Embed(bot))
