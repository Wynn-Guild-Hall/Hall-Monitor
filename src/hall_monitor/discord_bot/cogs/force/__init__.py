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

    @commands.group(name="force", invoke_without_command=True)
    async def force(self, ctx: commands.Context) -> None:
        """override a piece of state by hand"""
        await command_help.reply_all(ctx, await command_help.render_group(ctx, self.force))

    @commands.group(name="unforce", invoke_without_command=True)
    async def unforce(self, ctx: commands.Context) -> None:
        """undo a `~force`"""
        await command_help.reply_all(ctx, await command_help.render_group(ctx, self.unforce))


async def redraw_roster(ctx: commands.Context) -> None:
    """Redraw the roster after a force command that ran cleanly.

    Every `~force`/`~unforce` moves something the roster prints — which
    guild is major, who represents it, who holds its slots — so this is
    attached to all of them at once rather than remembered per command.
    Hooking them one at a time is how `~force guild` and `~force major`
    shipped without one: the roster went stale and the command said
    nothing was wrong.

    **It has to be `Command.after_invoke`, not `Cog.cog_after_invoke`.**
    That was the first attempt and it never fired once: these
    sub-commands are attached to the group inside `register()`, so
    they're not class attributes, so they're not in `__cog_commands__`,
    so `Cog._inject` never sets their `.cog` — and `call_after_hooks`
    only reaches the cog hook `if cog is not None`. Three
    `~force assign`s in a row left the roster showing the state before
    all of them, with each command reporting success.

    Setting `.cog` by hand is the obvious repair and is wrong: discord.py
    builds `ctx.args = [ctx] if self.cog is None else [self.cog, ctx]`,
    so it would start injecting the cog as the first positional argument
    into callbacks that don't take one.

    The sync is debounced and a pass with nothing to do is a single
    channel read, so redrawing after a command that happened to change
    nothing costs effectively nothing. After-hooks run in a `finally`,
    so this fires on failure too — hence the guard.
    """
    if ctx.command_failed or ctx.guild is None:
        return
    roster.request_sync(ctx.guild)


async def setup(bot: commands.Bot) -> None:
    from . import assign, expel, guild, invite, major, observer, rep

    cog = ForceGroup(bot)
    # Each sibling module attaches its `~force <name>` and `~unforce <name>` pair.
    # `rep` is the exception with no `~unforce`: it rewrites the row rather
    # than sitting in front of it, and there's no remembered previous guild
    # to go back to. Re-running it is the undo.
    for module in (assign, major, guild, observer, expel, rep, invite):
        module.register(cog)

    # Attached after registration and by walking the tree, so a
    # sub-command added later is covered without anybody remembering to —
    # which was the whole point of putting this in one place.
    for command in (*cog.force.walk_commands(), *cog.unforce.walk_commands()):
        command.after_invoke(redraw_roster)

    await bot.add_cog(cog)
