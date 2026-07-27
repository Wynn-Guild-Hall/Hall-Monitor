"""``~script reconcile`` — settle roles against notability without waiting.

The same pass the hourly job runs: contact roles withdrawn from guilds
that aren't notable and handed back to the ones that are, guild roles
recoloured or greyed, and spent guild roles recycled.

It reads the notability cache rather than refreshing it, so a stale cache
gives a stale answer — run `~script refresh_notability` first if that
matters.
"""

from hall_monitor.services import transitions


async def main(ctx, *args: str) -> None:
    if ctx.guild is None:
        await ctx.reply("run this in the server, not a DM — it edits roles.")
        return

    message = await ctx.reply("reconcile: working through the guilds…")
    summary = await transitions.reconcile(ctx.guild)
    await message.edit(content=f"reconcile done — {summary.line()}")
