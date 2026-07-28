"""``~script emotes`` — reconcile the guild banner emotes now.

The same pass the hourly job runs after the roster, and the one a boost
change triggers: mint banners for as many of the most strongly notable
guilds as there are free emote slots, evict the ones that have dropped
below the line, and put the same image on each guild's role as its
display icon where the boost level allows one.

Reads the notability cache rather than refreshing it, so both the
ordering and the count come from whatever the last sweep decided.
"""

from hall_monitor.config import settings
from hall_monitor.services import emote_slots


async def main(ctx, *args: str) -> None:
    if ctx.guild is None:
        await ctx.reply("run this in the server, not a DM — it uploads emotes.")
        return
    if not settings.roster_emotes_enabled:
        await ctx.reply("`ROSTER_EMOTES_ENABLED` is off, so I won't touch the list.")
        return

    slots = await emote_slots.budget(ctx.guild)
    if not slots:
        await ctx.reply(
            f"no free emote slots — the server allows {ctx.guild.emoji_limit}, and "
            "between what's already uploaded and `ROSTER_EMOTE_RESERVE` there's "
            "nothing spare. A boost would raise the limit."
        )
        return

    message = await ctx.reply(f"emotes: rendering banners for {slots} slot(s)…")
    summary = await emote_slots.reconcile(ctx.guild)
    await message.edit(content=f"emotes done — {summary.line()}")
