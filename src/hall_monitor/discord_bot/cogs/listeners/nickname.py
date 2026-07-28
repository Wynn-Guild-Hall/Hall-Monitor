"""Keep the ``Username [TAG]`` nickname shape on every member update.

The shape and the rule for who it applies to live in
``services/nicknames.py``; this is just the event plumbing.

``on_member_update`` fires for far more than nicknames — once per role
added, so a single verification fires it five times — and the listener is
called for every member of the server. Both filters below matter: the
early return keeps unrelated updates (avatar, timeout, presence-adjacent
changes) from touching the database at all, and ``enforce`` makes no
Discord request when the nickname is already right. Our own rename comes
back through here as another update, which is exactly why that second
check has to be the thing that stops it rather than any flag.
"""

import discord
from discord.ext import commands

from hall_monitor.services import nicknames


class Nickname(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if before.nick == after.nick and before.roles == after.roles:
            return  # nothing that could change the answer
        await nicknames.enforce(after)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Nickname(bot))
