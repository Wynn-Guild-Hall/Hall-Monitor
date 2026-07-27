"""Enforce the ``Username [TAG]`` nickname shape on member update events.

Stage 10 fills this in. Until then the listener is registered but does
nothing, deliberately: ``on_member_update`` fires once per role added, so
raising here logged five tracebacks every time a delegate was verified
and buried the line saying the join had worked. A stub that answers a
command only misbehaves when someone runs it; a stub on an event fires
whether anyone asked or not.
"""

import discord
from discord.ext import commands


class Nickname(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        return  # Stage 10


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Nickname(bot))
