"""``~help`` — replaces discord.py's built-in help with a Hall-Monitor-tailored one.

Scoped to the caller: a delegate shouldn't have to read past a wall of
janitor commands to find the two they can run, and advertising a command
someone can't use is a support question waiting to happen.
"""

from discord.ext import commands

from hall_monitor.discord_bot import command_help


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="help")
    async def help(self, ctx: commands.Context) -> None:
        """list the commands you can run"""
        await command_help.reply_all(ctx, await command_help.render_all(ctx, self.bot))


async def setup(bot: commands.Bot) -> None:
    bot.remove_command("help")
    await bot.add_cog(Help(bot))
