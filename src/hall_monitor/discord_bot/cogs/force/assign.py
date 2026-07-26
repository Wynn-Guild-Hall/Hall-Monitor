"""``~force assign`` — reassign a contact role to a delegate.

Ownership contacts can call this scoped to their own guild; janitors and
monitors can call it against any guild.
"""

from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_janitor, is_ownership_contact


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="assign")
    @commands.check_any(is_ownership_contact(), is_janitor())
    async def force_assign(ctx: commands.Context, user: str, contact_type: str) -> None:
        raise NotImplementedError

    @cog.unforce.command(name="assign")
    @commands.check_any(is_ownership_contact(), is_janitor())
    async def unforce_assign(ctx: commands.Context, user: str, contact_type: str) -> None:
        raise NotImplementedError
