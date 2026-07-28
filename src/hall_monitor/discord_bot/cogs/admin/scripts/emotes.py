"""``~script emotes [TAG]`` — reconcile the guild banner emotes now.

Without an argument: the same pass the hourly job runs after the roster,
and the one a boost change triggers. Mint banners for as many of the most
strongly major guilds as there are free emote slots, evict the ones
that have dropped below the line, and put the same image on each guild's
role as its display icon where the boost level allows one.

It runs to completion, which on a fresh server is a few minutes —
banners are fetched at a trickle to stay well inside Wynnpool's per-IP
limit — so it reports progress into its own message as it goes rather
than making anybody run it repeatedly.

With a guild tag: re-fetch **that** guild's banner immediately, ignoring
the usual re-check window. Guilds redesign about twice a year, so polling
for it often would be thousands of requests a year to catch a handful of
events — this is the path for when somebody has already noticed one.

Reads the major-guild cache rather than refreshing it, so both the
ordering and the count come from whatever the last sweep decided.
"""

import time

from hall_monitor.config import settings
from hall_monitor.services import emote_slots, guild_tag as tags, roster

# Discord allows roughly 5 edits per 5 s on a channel, and a full server
# is dozens of guilds — so throttle well clear of it.
_EDIT_INTERVAL_S = 5.0


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
    last_edit = time.monotonic()

    async def on_progress(done: int, total: int) -> None:
        # Wynnpool is fetched at a trickle, so a full server is minutes of
        # work. Reporting into the same message is what makes that a
        # background job rather than something to sit and watch.
        nonlocal last_edit
        now = time.monotonic()
        if now - last_edit < _EDIT_INTERVAL_S and done != total:
            return
        last_edit = now
        await message.edit(content=f"emotes: {done}/{total} guilds…")

    summary = await emote_slots.reconcile(ctx.guild, on_progress=on_progress)
    await message.edit(content=f"emotes done — {summary.line()}")


async def _refresh_one(ctx, wanted: str) -> None:
    """Force one guild's banner, for when a redesign has been spotted."""
    listed = [
        one for one in await roster.listed_guilds() if tags.matches(one.tag, wanted)
    ]
    if not listed:
        await ctx.reply(f"`{wanted}` isn't a major guild I know about.")
        return
    tag = listed[0].tag

    if not await emote_slots.holds_emote(ctx.guild, tag):
        await ctx.reply(
            f"`{tag}` doesn't hold one of the emote slots, so there's no banner "
            "to refresh — it wears the blank one. It gets a real banner by "
            "climbing the major-guild ranking, or by the server gaining a boost."
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
