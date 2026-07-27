"""``~script <name>`` — dispatches to modules under ``admin/scripts/``."""

from discord.ext import commands

from hall_monitor.discord_bot.cogs.admin.scripts import _loader
from hall_monitor.discord_bot.permissions import is_monitor


class Script(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="script")
    @is_monitor()
    async def script(self, ctx: commands.Context, name: str, *args: str) -> None:
        """run a one-off script from admin/scripts/"""
        await _loader.run_script(name, ctx, *args)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Script(bot))
