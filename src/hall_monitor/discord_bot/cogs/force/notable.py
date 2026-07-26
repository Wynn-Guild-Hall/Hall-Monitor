"""``~force notable`` — override a guild's notability status for a duration."""

from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_janitor


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="notable")
    @is_janitor()
    async def force_notable(ctx: commands.Context, guild_tag: str, duration: str) -> None:
        raise NotImplementedError

    @cog.unforce.command(name="notable")
    @is_janitor()
    async def unforce_notable(ctx: commands.Context, guild_tag: str) -> None:
        raise NotImplementedError
