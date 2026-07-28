"""``~script render_banner <TAG>`` — render a guild's banner and show it.

The inspection path for DESIGN.md §15. It runs the same render the emote
reconcile does, but posts the PNG as an attachment instead of uploading
it, so a banner can be checked against the in-game article without
spending one of the server's emote slots on finding out.

Reports the hash too, which is what decides whether the hourly pass
re-uploads: a render that comes back with the same digest is one the
reconcile will leave completely alone.
"""

import io

import discord

from hall_monitor.services import banner_render, emote_slots, guild_tag as tags, roster


async def main(ctx, *args: str) -> None:
    if not args:
        await ctx.reply("usage: `~script render_banner <TAG>`")
        return
    wanted = args[0]

    listed = [one for one in await roster.listed_guilds() if tags.matches(one.tag, wanted)]
    if not listed:
        await ctx.reply(
            f"`{wanted}` isn't a notable guild I know about — "
            "`~script refresh_notability` first if it should be."
        )
        return
    one = listed[0]
    if tags.matches(one.name, one.tag):
        await ctx.reply(
            f"I don't have a full name for `{one.tag}`, and Wynnpool's banner "
            "endpoint only takes names. `~script refresh_notability` resolves it."
        )
        return

    message = await ctx.reply(f"rendering `{one.tag}`…")
    png = await emote_slots.rendered_banner(one.tag, one.name)
    if png is None:
        await message.edit(content=f"Wynnpool has no banner for **{one.name}**.")
        return
    await message.edit(
        content=(
            f"**{one.name}** (`{one.tag}`) — {len(png)} bytes, "
            f"hash `{banner_render.image_hash(png)[:12]}`"
        ),
        attachments=[discord.File(io.BytesIO(png), filename=f"{one.tag}.png")],
    )
