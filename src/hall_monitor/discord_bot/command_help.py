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


# Discord's cap on a message body. The same number `services/roster.py`
# works to, and deliberately a separate constant: they're independent
# consumers of one Discord limit, not a shared policy that should move
# together.
MESSAGE_LIMIT = 2000


def chunk(blocks: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Pack rendered blocks into messages, never splitting one.

    A **block** is a top-level command, or a group together with its
    indented subcommands. Splitting a group across a boundary would open
    the next message with orphaned subcommands under nothing, reading as
    commands in their own right.

    Same shape as ``roster.chunk``, for the same reason: Discord refuses
    a message over 2000 characters outright, with a 400 rather than a
    truncation.
    """
    messages: list[str] = []
    current: list[str] = []
    length = 0
    for block in blocks:
        if len(block) > limit:
            # Not reachable with one command per line, but a truncated
            # entry beats a message Discord refuses and a listing that
            # stops dead here.
            block = block[:limit]
        separator = 1 if current else 0  # the newline between blocks
        if current and length + separator + len(block) > limit:
            messages.append("\n".join(current))
            current, length, separator = [], 0, 0
        current.append(block)
        length += separator + len(block)
    if current:
        messages.append("\n".join(current))
    return messages


async def reply_all(ctx: commands.Context, messages: list[str]) -> None:
    """Reply with the first message, then follow it with the rest in order."""
    for index, body in enumerate(messages):
        if index == 0:
            await ctx.reply(body)
        else:
            await ctx.send(body)


async def render_group(ctx: commands.Context, group: commands.Group) -> list[str]:
    """What a bare ``~force`` should say back, in message-sized pieces."""
    subcommands = await visible(ctx, group)
    name = f"{ctx.clean_prefix}{group.qualified_name}"
    if not subcommands:
        return [f"there's no `{name}` subcommand you can run."]
    blocks = [f"**{name}** — needs a subcommand:"]
    blocks += [f"· {describe(ctx, sub)}" for sub in subcommands]
    return chunk(blocks)


async def render_all(ctx: commands.Context, bot: commands.Bot) -> list[str]:
    """The whole ``~help`` body, scoped to what the caller can run.

    Returns **a list of messages**, not one string. Every command being
    built (Stage 16) took a monitor's listing past 2000 characters, and
    Discord answers that with a 400 rather than a truncation — so `~help`
    stopped working entirely, and only for the person most likely to need
    it, which is why it took a deploy to notice.
    """
    blocks = []
    for command in await visible(ctx, bot):
        if isinstance(command, commands.Group):
            # A group whose every subcommand is gated away from this
            # caller is noise to them — `~force` with nothing under it
            # reads as a command they can run, and it isn't.
            subcommands = await visible(ctx, command)
            if not subcommands:
                continue
            # One block: a group and its subcommands travel together, or
            # the next message opens with indented orphans.
            blocks.append(
                "\n".join(
                    [f"· {describe(ctx, command)}"]
                    + [f"    · {describe(ctx, sub)}" for sub in subcommands]
                )
            )
            continue
        blocks.append(f"· {describe(ctx, command)}")
    if not blocks:
        return [
            "you can't run anything yet — representatives verify at "
            "<https://hall.wynnvets.org/join>."
        ]
    return chunk(["**Guild Hall commands** — what you can run right now."] + blocks)


def describe(ctx: commands.Context, command: commands.Command) -> str:
    line = signature(ctx, command)
    return f"{line} — {command.short_doc}" if command.short_doc else line
