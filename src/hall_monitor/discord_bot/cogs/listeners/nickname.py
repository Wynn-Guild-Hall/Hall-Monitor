"""Enforce the ``Username [TAG]`` nickname shape on member update events."""

import discord
from discord.ext import commands


class Nickname(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        raise NotImplementedError


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Nickname(bot))
