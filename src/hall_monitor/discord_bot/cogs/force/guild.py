"""``~force guild`` — treat a user as representative of a guild they're not in."""

from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_janitor


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="guild")
    @is_janitor()
    async def force_guild(ctx: commands.Context, user: str, guild_tag: str, duration: str) -> None:
        raise NotImplementedError

    @cog.unforce.command(name="guild")
    @is_janitor()
    async def unforce_guild(ctx: commands.Context, user: str) -> None:
        raise NotImplementedError
