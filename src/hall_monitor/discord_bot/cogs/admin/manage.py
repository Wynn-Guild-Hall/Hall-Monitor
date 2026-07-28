"""``~manage`` — monitor-only commands for the bot itself rather than the Hall.

Everything under ``~force`` changes what the Hall *is*; everything here
changes what the process is doing. The split is worth keeping when
reading back an audit log: ``~force expel`` is a decision,
``~manage reload_cogs`` is maintenance.

Two of these deliberately **delegate to the ``~script`` modules** rather
than reimplementing them. ``~manage refresh_major`` and
``~script refresh_major`` have to mean the same thing, and one of
them throttling its progress edits while the other doesn't is exactly the
divergence nobody notices until an operator reports different behaviour
from two commands that sound identical. The scripts own the work; these
are aliases with a name a monitor can guess.
"""

import logging
import os
import signal

from discord.ext import commands

from hall_monitor.discord_bot import _discover_cog_modules, command_help
from hall_monitor.discord_bot.permissions import is_monitor
from hall_monitor.services import roster

logger = logging.getLogger(__name__)


class Manage(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.group(name="manage", invoke_without_command=True)
    @is_monitor()
    async def manage(self, ctx: commands.Context) -> None:
        """look after the bot process itself"""
        await command_help.reply_all(ctx, await command_help.render_group(ctx, self.manage))

    @manage.command(name="reload_cogs")
    @is_monitor()
    async def reload_cogs(self, ctx: commands.Context) -> None:
        """re-import every cog without restarting the process"""
        reloaded, failed = [], []
        for name in _discover_cog_modules("hall_monitor.discord_bot.cogs"):
            try:
                await self.bot.reload_extension(name)
            except commands.ExtensionNotLoaded:
                continue  # no `setup()` — a helper module, not a cog
            except Exception:
                logger.exception("manage: couldn't reload %s", name)
                failed.append(name.rsplit(".", 1)[-1])
            else:
                reloaded.append(name.rsplit(".", 1)[-1])

        line = f"reloaded {len(reloaded)} cog(s)."
        if failed:
            # Named, because a partial reload leaves the process running
            # some old code and some new — and which is which decides
            # whether the next thing you try proves anything at all.
            line += (
                f" **{len(failed)} failed and kept their old code**: "
                + ", ".join(f"`{name}`" for name in failed)
                + ". `~manage shutdown` for a clean start."
            )
        await ctx.reply(line)

    @manage.command(name="refresh_major")
    @is_monitor()
    async def refresh_major(self, ctx: commands.Context) -> None:
        """re-evaluate which guilds are major now"""
        from hall_monitor.discord_bot.cogs.admin.scripts import (
            refresh_major as script,
        )

        await script.main(ctx)

    @manage.command(name="resync_roster")
    @is_monitor()
    async def resync_roster(self, ctx: commands.Context) -> None:
        """redraw the Current Guilds channel from the cache"""
        from hall_monitor.discord_bot.cogs.admin.scripts import roster as script

        await script.main(ctx)

    @manage.command(name="shutdown")
    @is_monitor()
    async def shutdown(self, ctx: commands.Context) -> None:
        """exit, so the container restarts me fresh"""
        # The stack runs `restart: unless-stopped`, so exiting *is* the
        # restart: Docker brings the container back with a clean process
        # and whatever image is on disk. Nothing here starts the bot again
        # if that policy is ever removed, which is why the reply says what
        # is about to happen rather than promising to be back.
        await ctx.reply(
            "shutting down. Docker's `restart: unless-stopped` should have me "
            "back within a few seconds — if I'm not, the container was started "
            "without that policy and needs `manage up hall-monitor` on the VPS."
        )
        logger.warning(
            "manage: %s (%s) asked me to exit; the container should restart me",
            ctx.author,
            ctx.author.id,
        )
        # Drain first. The roster sync is debounced and fire-and-forget, so
        # a shutdown seconds after a `~force` would otherwise drop a redraw
        # nothing else is going to make until the next hourly pass.
        await roster.wait_for_pending_sync()
        await self.bot.close()
        # SIGTERM rather than `sys.exit`: uvicorn is serving on another
        # task in the same loop and `bot.close()` doesn't stop it. This is
        # the signal `docker stop` sends, so the process takes its ordinary
        # shutdown path rather than a novel one that only this command uses.
        os.kill(os.getpid(), signal.SIGTERM)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Manage(bot))
