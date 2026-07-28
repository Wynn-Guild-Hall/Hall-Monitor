"""Turn a command error into something the invoker can act on.

discord.py's default is to log a traceback and tell the user nothing, so
every unfinished command reads in Discord as "the bot is broken" rather
than "that isn't built yet" — and a mistyped argument or a missing role
reads the same way. One handler covers the lot, including commands added
by later stages.
"""

import logging

import discord
from discord.ext import commands

from hall_monitor.discord_bot import command_help

logger = logging.getLogger(__name__)


async def handle(ctx: commands.Context, error: Exception) -> None:
    if isinstance(error, commands.CommandNotFound):
        return  # `~` starts plenty of ordinary sentences

    if ctx.command is not None and ctx.command.hidden:
        # `hidden` means not built yet (DESIGN.md §7), and that outranks
        # whatever the gate or the parser objected to first. A check runs
        # before the body, so an unbuilt gated command would otherwise
        # answer "you don't have the role" — sending someone off editing
        # roles for a command that would do nothing if they had them.
        await _reply(ctx, _not_built(ctx))
        return

    if isinstance(error, commands.CheckFailure):
        await _reply(ctx, "you don't have the role for that one.")
        return

    if isinstance(error, commands.BadArgument):
        # A converter that went to the trouble of explaining itself gets
        # to. Folding this into the usage line below is how `~force
        # assign Struppi0508 warring` came back as nothing but the syntax
        # the operator had already typed correctly — the reason it failed
        # (a name we couldn't resolve) never reached them.
        await _reply(
            ctx, f"{error}\nusage: {command_help.signature(ctx, ctx.command)}"
        )
        return

    if isinstance(error, commands.UserInputError):
        # Everything else here is a *shape* problem — a missing or extra
        # argument — where the usage line is the whole answer and the
        # library's own wording adds nothing.
        await _reply(ctx, f"usage: {command_help.signature(ctx, ctx.command)}")
        return

    # `.original` rather than `__cause__`: it's what CommandInvokeError
    # documents, and it survives an error re-raised without `from`. This
    # catches a stub that was never marked `hidden`.
    if isinstance(getattr(error, "original", None), NotImplementedError):
        await _reply(ctx, _not_built(ctx))
        return

    logger.error(
        "command %s raised", ctx.command and ctx.command.qualified_name, exc_info=error
    )
    await _reply(ctx, "that broke on my end — the details are in my logs.")


def _not_built(ctx: commands.Context) -> str:
    return f"`{ctx.clean_prefix}{ctx.command.qualified_name}` isn't built yet."


async def _reply(ctx: commands.Context, message: str) -> None:
    """Best-effort. A reply that itself fails must not re-enter the handler."""
    try:
        await ctx.reply(message)
    except discord.HTTPException:
        logger.exception("couldn't deliver the error reply for %s", ctx.command)
