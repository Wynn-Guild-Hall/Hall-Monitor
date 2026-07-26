"""``~force`` and ``~unforce`` command groups.

Sub-commands live in sibling modules and register themselves onto the groups
here so that per-sub-command permission gates (ownership contact vs. janitor
vs. monitor) stay adjacent to the logic they gate.
"""

from discord.ext import commands


class ForceGroup(commands.Cog):
    """Container cog that owns the two top-level command groups."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.group(name="force", invoke_without_command=True)
    async def force(self, ctx: commands.Context) -> None:
        raise NotImplementedError

    @commands.group(name="unforce", invoke_without_command=True)
    async def unforce(self, ctx: commands.Context) -> None:
        raise NotImplementedError


async def setup(bot: commands.Bot) -> None:
    from . import assign, expel, guild, notable, observer

    cog = ForceGroup(bot)
    # Each sibling module attaches its `~force <name>` and `~unforce <name>` pair.
    for module in (assign, notable, guild, observer, expel):
        module.register(cog)
    await bot.add_cog(cog)
