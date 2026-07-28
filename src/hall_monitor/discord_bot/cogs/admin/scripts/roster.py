"""``~script roster`` — redraw the Current Guilds channel now.

The same pass the hourly job runs after the reconcile: every notable
guild, in level-board order, with its four contacts. It reads the
notability cache rather than refreshing it, so a stale cache gives a
stale roster — run `~script refresh_notability` first if that matters.

Anything in the channel that isn't one of the bot's tracked roster
messages is deleted, which is what makes the first run tidy up whatever
was there before.
"""

from hall_monitor.config import settings
from hall_monitor.services import roster


async def main(ctx, *args: str) -> None:
    if ctx.guild is None:
        await ctx.reply("run this in the server, not a DM — it posts to a channel.")
        return
    if not settings.roster_channel_id:
        await ctx.reply(
            "`ROSTER_CHANNEL_ID` isn't set, so there's no channel to draw in."
        )
        return

    message = await ctx.reply("roster: rendering…")
    summary = await roster.sync_channel(ctx.guild)
    if summary is None:
        await message.edit(
            content=(
                f"roster: channel `{settings.roster_channel_id}` isn't reachable "
                "— check the ID and that I can read its history."
            )
        )
        return
    await message.edit(content=f"roster done — {summary.line()}")
