"""discord.py bot factory + cog auto-loader.

The loader walks ``cogs/`` and calls ``setup()`` on any module or package that
exposes one; files/packages named ``_*`` are skipped (used for private helpers
like ``cogs/admin/scripts/_loader.py``).
"""

import importlib
import logging
import pkgutil
from typing import Iterable

import discord
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.discord_bot import errors

log = logging.getLogger(__name__)


def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True
    bot = commands.Bot(command_prefix=settings.prefix, intents=intents)

    @bot.event
    async def on_command_error(ctx: commands.Context, error: Exception) -> None:
        await errors.handle(ctx, error)

    async def _setup_hook() -> None:
        for module_name in _discover_cog_modules("hall_monitor.discord_bot.cogs"):
            try:
                await bot.load_extension(module_name)
            except commands.NoEntryPointError:
                log.debug("skipping %s (no setup())", module_name)
            except Exception:
                log.exception("failed to load cog %s", module_name)

    bot.setup_hook = _setup_hook
    return bot


def _discover_cog_modules(package_name: str) -> Iterable[str]:
    package = importlib.import_module(package_name)
    for finder, name, _is_pkg in pkgutil.walk_packages(package.__path__, prefix=f"{package_name}."):
        leaf = name.rsplit(".", 1)[-1]
        if leaf.startswith("_"):
            continue
        yield name
