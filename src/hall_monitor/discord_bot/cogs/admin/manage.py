"""``~manage`` — monitor-only bot self-management commands (restart, etc.)."""

from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_monitor


class Manage(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.group(name="manage", invoke_without_command=True)
    @is_monitor()
    async def manage(self, ctx: commands.Context) -> None:
        raise NotImplementedError


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Manage(bot))
