"""``~force expel`` — remove a guild without going through the delegate vote.

Monitor-only, since it bypasses the community-vote flow that ``~expel_motion``
runs through for the delegate role.
"""

from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_monitor


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="expel", hidden=True)
    @is_monitor()
    async def force_expel(ctx: commands.Context, guild_tag: str) -> None:
        raise NotImplementedError

    @cog.unforce.command(name="expel", hidden=True)
    @is_monitor()
    async def unforce_expel(ctx: commands.Context, guild_tag: str) -> None:
        raise NotImplementedError
