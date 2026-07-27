"""``~script guild_role <TAG>`` — create or re-colour a guild's aesthetic role.

The role otherwise only appears when somebody from that guild verifies,
which makes "does this colour read on both themes?" a question you can
only answer by finding a chief willing to join. This runs exactly what
the join path runs — Athena lookup, contrast clamp, create-or-edit —
against any tag, and mentions the result so it renders in the channel.
"""

from hall_monitor.services import athena_colour, guild_roles


async def main(ctx, *args: str) -> None:
    if ctx.guild is None:
        await ctx.reply("run this in the server, not a DM — it edits roles.")
        return
    if not args:
        await ctx.reply('usage: `~script guild_role <TAG>` (quote a tag with a space)')
        return

    tag = " ".join(args)
    existed = guild_roles.find_guild_role(ctx.guild, tag) is not None
    colour = await athena_colour.colour_for(tag)
    role = await guild_roles.ensure_guild_role(
        ctx.guild, tag, colour_hex=colour, reason=f"hall-monitor: ~script by {ctx.author.id}"
    )
    if role is None:
        await ctx.reply(
            f"couldn't create the `{tag}` role — check Manage Roles and where "
            "the bot's own role sits."
        )
        return

    note = (
        " — Athena doesn't know this guild, so that's the fallback colour"
        if colour == athena_colour.DEFAULT_COLOUR
        else ""
    )
    verb = "already there" if existed else "created"
    await ctx.reply(f"{role.mention} {verb}, colour `{colour}`{note}")
