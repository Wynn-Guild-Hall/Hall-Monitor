"""``~expel_motion`` — start a community vote to remove a guild from the server."""

from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_delegate


class ExpelMotion(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="expel_motion")
    @is_delegate()
    async def expel_motion(self, ctx: commands.Context, guild_tag: str) -> None:
        raise NotImplementedError


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ExpelMotion(bot))
