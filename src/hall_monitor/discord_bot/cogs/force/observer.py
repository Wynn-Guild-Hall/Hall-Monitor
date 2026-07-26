"""``~force observer`` — mint an invite for a non-representative observer."""

from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_janitor


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="observer")
    @is_janitor()
    async def force_observer(ctx: commands.Context, username: str) -> None:
        raise NotImplementedError

    @cog.unforce.command(name="observer")
    @is_janitor()
    async def unforce_observer(ctx: commands.Context, username: str) -> None:
        raise NotImplementedError
