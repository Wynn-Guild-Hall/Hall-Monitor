"""``~script emotes`` — reconcile the guild banner emotes now.

The same pass the hourly job runs after the roster: mint banners for the
guilds inside `ROSTER_EMOTE_BUDGET`, evict the ones that have dropped
below it, and put the same image on each guild's role as its display icon
where the server's boost level allows one.

Reads the notability cache rather than refreshing it, so the ordering it
spends the budget on is whatever the last sweep decided.
"""

from hall_monitor.config import settings
from hall_monitor.services import emote_slots


async def main(ctx, *args: str) -> None:
    if ctx.guild is None:
        await ctx.reply("run this in the server, not a DM — it uploads emotes.")
        return
    if not settings.roster_emote_budget:
        await ctx.reply(
            "`ROSTER_EMOTE_BUDGET` is 0, so I won't take any emote slots. "
            "Set it to the number of slots I may use and try again."
        )
        return

    message = await ctx.reply("emotes: rendering banners…")
    summary = await emote_slots.reconcile(ctx.guild)
    await message.edit(content=f"emotes done — {summary.line()}")
