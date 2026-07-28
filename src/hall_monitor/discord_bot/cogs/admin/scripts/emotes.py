"""``~script emotes [TAG]`` — reconcile the guild banner emotes now.

Without an argument: the same pass the hourly job runs after the roster,
and the one a boost change triggers. Mint banners for as many of the most
strongly notable guilds as there are free emote slots, evict the ones
that have dropped below the line, and put the same image on each guild's
role as its display icon where the boost level allows one.

With a guild tag: re-fetch **that** guild's banner immediately, ignoring
the usual re-check window. Guilds redesign about twice a year, so polling
for it often would be thousands of requests a year to catch a handful of
events — this is the path for when somebody has already noticed one.

Reads the notability cache rather than refreshing it, so both the
ordering and the count come from whatever the last sweep decided.
"""

from hall_monitor.config import settings
from hall_monitor.services import emote_slots, guild_tag as tags, roster


async def main(ctx, *args: str) -> None:
    if ctx.guild is None:
        await ctx.reply("run this in the server, not a DM — it uploads emotes.")
        return
    if not settings.roster_emotes_enabled:
        await ctx.reply("`ROSTER_EMOTES_ENABLED` is off, so I won't touch the list.")
        return

    if args:
        await _refresh_one(ctx, args[0])
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


async def _refresh_one(ctx, wanted: str) -> None:
    """Force one guild's banner, for when a redesign has been spotted."""
    listed = [
        one for one in await roster.listed_guilds() if tags.matches(one.tag, wanted)
    ]
    if not listed:
        await ctx.reply(f"`{wanted}` isn't a notable guild I know about.")
        return
    tag = listed[0].tag

    if not await emote_slots.holds_emote(ctx.guild, tag):
        await ctx.reply(
            f"`{tag}` doesn't hold one of the emote slots, so there's no banner "
            "to refresh — it wears the blank one. It gets a real banner by "
            "climbing the notability ranking, or by the server gaining a boost."
        )
        return

    message = await ctx.reply(f"emotes: re-fetching `{tag}`'s banner…")
    summary = await emote_slots.refresh_guild(ctx.guild, tag)
    if summary.refreshed:
        await message.edit(content=f"`{tag}`'s banner changed — replaced it.")
    elif summary.failed:
        await message.edit(content=f"couldn't refresh `{tag}` — see the logs.")
    else:
        await message.edit(content=f"`{tag}`'s banner is unchanged; nothing to do.")
