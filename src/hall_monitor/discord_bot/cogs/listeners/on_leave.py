"""A representative leaving the server — mark the row, redraw the roster.

The counterpart to ``on_join``, and deliberately much smaller. Leaving
sets ``Delegate.left_at`` and nothing else: the ``GuildContact`` rows stay
put, on the same reasoning as everywhere else in the Hall (DESIGN.md §6).
Somebody who leaves and comes back gets their slots back, and until then
the display side already treats a slot held by an absent member as
unclaimed — ``contacts.current_contacts_for_guild`` cross-checks against
the server, so nothing shows a contact who isn't there.

Marking the row is what stops the hourly guild watch spending a Wynncraft
request per sweep on somebody who has gone, and what keeps the reconcile
from counting them as a live presence for their guild.

Note this fires for a kick as well as a leave — including the one
``services/contacts.py`` performs when a displaced contact is left with
nothing. That path already calls ``mark_left`` itself; doing it twice
writes the same timestamp shape and costs nothing.
"""

import logging

import discord
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.db.models import Observer
from hall_monitor.services import delegate_registry, roster

logger = logging.getLogger(__name__)


class OnLeave(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if settings.discord_guild_id and member.guild.id != settings.discord_guild_id:
            return
        if member.bot:
            return

        # An observer's record goes outright rather than being marked
        # left. It exists to bind a Discord account to a Minecraft one,
        # and once the account is gone the binding is about nobody —
        # unlike a `Delegate` row, which the Hall keeps so a return costs
        # no re-verification and so the history stays readable.
        if await Observer.filter(discord_user_id=member.id).delete():
            logger.info("leave: %s left the server; observer record dropped", member.id)
            return

        delegate = await delegate_registry.get_by_discord_user_id(member.id)
        if delegate is None:
            return  # a guest, or staff — the Hall has no record to close

        if delegate.left_at is None:
            await delegate_registry.mark_left(member.id)
            logger.info(
                "leave: %s left the server; %s representative row marked",
                member.id,
                delegate.guild_tag,
            )
        roster.request_sync(member.guild)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OnLeave(bot))
