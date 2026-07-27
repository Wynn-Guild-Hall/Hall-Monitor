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

    if isinstance(error, commands.CheckFailure):
        await _reply(ctx, "you don't have the role for that one.")
        return

    if isinstance(error, commands.UserInputError):
        await _reply(ctx, f"usage: {command_help.signature(ctx, ctx.command)}")
        return

    # `.original` rather than `__cause__`: it's what CommandInvokeError
    # documents, and it survives an error re-raised without `from`.
    if isinstance(getattr(error, "original", None), NotImplementedError):
        name = f"{ctx.clean_prefix}{ctx.command.qualified_name}"
        await _reply(ctx, f"`{name}` isn't built yet.")
        return

    logger.error(
        "command %s raised", ctx.command and ctx.command.qualified_name, exc_info=error
    )
    await _reply(ctx, "that broke on my end — the details are in my logs.")


async def _reply(ctx: commands.Context, message: str) -> None:
    """Best-effort. A reply that itself fails must not re-enter the handler."""
    try:
        await ctx.reply(message)
    except discord.HTTPException:
        logger.exception("couldn't deliver the error reply for %s", ctx.command)
