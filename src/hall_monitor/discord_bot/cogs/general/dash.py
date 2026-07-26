"""``~dash`` — contact-owned key/value store surfaced to the Hallway site."""

from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_contact


class Dash(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.group(name="dash", invoke_without_command=True)
    @is_contact()
    async def dash(self, ctx: commands.Context) -> None:
        raise NotImplementedError

    @dash.command(name="toggle")
    async def toggle(self, ctx: commands.Context, key: str, value: bool) -> None:
        raise NotImplementedError

    @dash.command(name="set")
    async def set_(self, ctx: commands.Context, key: str, *, value: str) -> None:
        raise NotImplementedError

    @dash.command(name="add")
    async def add(self, ctx: commands.Context, key: str, *, value: str) -> None:
        raise NotImplementedError


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Dash(bot))
