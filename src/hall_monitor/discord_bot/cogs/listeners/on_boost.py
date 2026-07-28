"""React to the server's boost level moving, in either direction.

Two things scale with it and both are ours to keep in step:

- **Emote slots.** 50 at tier 0, 100 at 1, 150 at 2, 250 at 3. Guild
  banners fill whatever the server isn't otherwise using, so a level
  gained is room for more of the roster and a level lost means the tail
  has to be evicted — Discord doesn't remove the overflow itself, it
  just refuses every subsequent upload, which would look like the mint
  silently failing.
- **Role icons.** Level 2 grants `ROLE_ICONS`; below it every
  `display_icon` write is a 403 and Discord strips the icons already
  set. The recorded hashes are cleared on the way down so that regaining
  the level puts them back, rather than every role reading as "already
  correct" and staying bare (`guild_roles.sync_role_icon`).

The hourly pass would reach the same state on its own — `budget` is
derived from `emoji_limit` every time it runs, and none of this is
edge-triggered state. This listener exists so the wait isn't an hour:
a boost is a thing somebody *paid for* and then watches for, and "it'll
sort itself out within the hour" is a poor answer to that.

Discord sends `GUILD_UPDATE` for every field on the guild — name, icon,
vanity URL, AFK timeout. Acting on all of them would run a full emote
reconcile whenever someone renamed the server, so this fires only when
the slot count or the role-icon feature actually moved.
"""

import logging

import discord
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.services import emote_slots, guild_roles

logger = logging.getLogger(__name__)


def _relevant_change(before: discord.Guild, after: discord.Guild) -> str | None:
    """What moved that we care about, as a phrase for the log."""
    changes = []
    if before.emoji_limit != after.emoji_limit:
        changes.append(f"emote slots {before.emoji_limit} → {after.emoji_limit}")
    if guild_roles.has_role_icons(before) != guild_roles.has_role_icons(after):
        changes.append(
            "role icons "
            + ("available" if guild_roles.has_role_icons(after) else "gone")
        )
    return ", ".join(changes) or None


class OnBoost(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_update(
        self, before: discord.Guild, after: discord.Guild
    ) -> None:
        if settings.discord_guild_id and after.id != settings.discord_guild_id:
            return
        change = _relevant_change(before, after)
        if change is None:
            return

        logger.info(
            "boost: tier %s → %s (%s); reconciling banners",
            before.premium_tier,
            after.premium_tier,
            change,
        )
        try:
            summary = await emote_slots.reconcile(after)
        except Exception:  # noqa: BLE001 — a listener with nobody to raise to
            logger.exception("boost: emote reconcile failed")
            return
        logger.info("emotes: %s", summary.line())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OnBoost(bot))
