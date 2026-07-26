"""Resolve which single-use invite a new member joined through, then apply their bound roles."""

import discord
from discord.ext import commands


class OnJoin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        raise NotImplementedError


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OnJoin(bot))
