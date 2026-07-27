"""Rendering shared by ``~help`` and by a bare group invocation.

Both answer the same question — *what can I actually run?* — so both
build their list from the live command tree and filter it through each
command's own checks, rather than restating a fixed list that drifts.

A command that isn't built yet is registered ``hidden=True``. That flag
is the single marker keeping it out of both surfaces; implementing the
command means dropping it.
"""

from discord.ext import commands


async def runnable(ctx: commands.Context, command: commands.Command) -> bool:
    """Whether ``ctx.author`` passes ``command``'s checks.

    ``can_run`` returns ``False`` for a plain failing predicate but
    *raises* for composite ones like ``check_any``. Both mean the same
    thing to a help listing.
    """
    try:
        return await command.can_run(ctx)
    except commands.CommandError:
        return False


def signature(ctx: commands.Context, command: commands.Command) -> str:
    """``~force assign <user> <contact_type>``, backticked."""
    line = f"{ctx.clean_prefix}{command.qualified_name} {command.signature}".rstrip()
    return f"`{line}`"


async def visible(
    ctx: commands.Context, parent: commands.Group | commands.Bot
) -> list[commands.Command]:
    """``parent``'s commands that are both built and runnable by the caller."""
    shown = []
    for command in sorted(parent.commands, key=lambda c: c.qualified_name):
        if command.hidden:
            continue
        if not await runnable(ctx, command):
            continue
        shown.append(command)
    return shown


async def render_group(ctx: commands.Context, group: commands.Group) -> str:
    """What a bare ``~force`` should say back."""
    subcommands = await visible(ctx, group)
    name = f"{ctx.clean_prefix}{group.qualified_name}"
    if not subcommands:
        return f"there's no `{name}` subcommand you can run."
    lines = [f"**{name}** — needs a subcommand:"]
    lines += [f"· {describe(ctx, sub)}" for sub in subcommands]
    return "\n".join(lines)


async def render_all(ctx: commands.Context, bot: commands.Bot) -> str:
    """The whole ``~help`` body, scoped to what the caller can run."""
    lines = []
    for command in await visible(ctx, bot):
        if isinstance(command, commands.Group):
            # A group whose every subcommand is gated away from this
            # caller is noise to them — `~force` with nothing under it
            # reads as a command they can run, and it isn't.
            subcommands = await visible(ctx, command)
            if not subcommands:
                continue
            lines.append(f"· {describe(ctx, command)}")
            lines += [f"    · {describe(ctx, sub)}" for sub in subcommands]
            continue
        lines.append(f"· {describe(ctx, command)}")
    if not lines:
        return (
            "you can't run anything yet — representatives verify at "
            "<https://hall.wynnvets.org/join>."
        )
    return "\n".join(["**Guild Hall commands** — what you can run right now."] + lines)


def describe(ctx: commands.Context, command: commands.Command) -> str:
    line = signature(ctx, command)
    return f"{line} — {command.short_doc}" if command.short_doc else line
