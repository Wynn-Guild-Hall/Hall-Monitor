"""``~help`` — replaces discord.py's built-in help with a Hall-Monitor-tailored one."""

from discord.ext import commands


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="help")
    async def help(self, ctx: commands.Context) -> None:
        raise NotImplementedError


async def setup(bot: commands.Bot) -> None:
    bot.remove_command("help")
    await bot.add_cog(Help(bot))
