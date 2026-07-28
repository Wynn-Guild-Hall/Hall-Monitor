"""``~force`` and ``~unforce`` command groups.

Sub-commands live in sibling modules and register themselves onto the groups
here so that per-sub-command permission gates (ownership contact vs. janitor
vs. monitor) stay adjacent to the logic they gate.
"""

from discord.ext import commands

from hall_monitor.discord_bot import command_help
from hall_monitor.services import roster


class ForceGroup(commands.Cog):
    """Container cog that owns the two top-level command groups."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_after_invoke(self, ctx: commands.Context) -> None:
        """Redraw the roster after any force command that ran cleanly.

        Every `~force`/`~unforce` moves something the roster prints —
        which guild is major, who represents it, who holds its slots —
        so the hook belongs on the group rather than on each sub-command.
        Hooking them one at a time is how `~force guild` and
        `~force major` shipped without one: the roster went stale and
        the command said nothing was wrong. This way a sub-command added
        later, including the two Stage 14 and 15 still owe, is covered
        without anyone remembering to.

        The sync is debounced and a pass with nothing to do is a single
        channel read, so redrawing after a command that happened not to
        change anything costs effectively nothing.
        """
        if ctx.command_failed or ctx.guild is None:
            return
        roster.request_sync(ctx.guild)

    @commands.group(name="force", invoke_without_command=True)
    async def force(self, ctx: commands.Context) -> None:
        """override a piece of state by hand"""
        await command_help.reply_all(ctx, await command_help.render_group(ctx, self.force))

    @commands.group(name="unforce", invoke_without_command=True)
    async def unforce(self, ctx: commands.Context) -> None:
        """undo a `~force`"""
        await command_help.reply_all(ctx, await command_help.render_group(ctx, self.unforce))


async def setup(bot: commands.Bot) -> None:
    from . import assign, expel, guild, invite, major, observer, rep

    cog = ForceGroup(bot)
    # Each sibling module attaches its `~force <name>` and `~unforce <name>` pair.
    # `rep` is the exception with no `~unforce`: it rewrites the row rather
    # than sitting in front of it, and there's no remembered previous guild
    # to go back to. Re-running it is the undo.
    for module in (assign, major, guild, observer, expel, rep, invite):
        module.register(cog)
    await bot.add_cog(cog)
