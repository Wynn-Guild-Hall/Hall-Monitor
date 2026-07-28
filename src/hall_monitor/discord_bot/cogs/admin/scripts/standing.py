"""``~script standing @user`` — everything the bot believes about a member.

Written after an afternoon of "why does she still have VETS?", which was
only answerable by reading logs and guessing. The three questions that
actually matter are: what standing does the bot compute, which role does
it resolve for their guild, and is that role one it's *allowed* to take
off them. Role position answers the third and is invisible from the
outside — Administrator doesn't waive it.
"""

import re

from hall_monitor.db.models import GuildContact
from hall_monitor.services import delegate_registry, guild_roles, notability

_MENTION = re.compile(r"[<@!&>]")


def _user_id(argument: str) -> int | None:
    digits = _MENTION.sub("", argument).strip()
    return int(digits) if digits.isdigit() else None


def _holds(member, role) -> bool:
    return role is not None and any(r.id == role.id for r in member.roles)


async def main(ctx, *args: str) -> None:
    if ctx.guild is None or not args:
        await ctx.reply("usage: `~script standing @user` (in the server).")
        return
    user_id = _user_id(args[0])
    member = ctx.guild.get_member(user_id) if user_id else None
    if member is None:
        await ctx.reply(f"`{args[0]}` isn't a member of this server.")
        return

    delegate = await delegate_registry.get_by_discord_user_id(member.id)
    if delegate is None:
        await ctx.reply(f"{member.mention} has no delegate row — nothing to settle.")
        return

    forced = await delegate_registry.forced_guild(member.id)
    notable = await notability.is_notable(delegate.guild_tag)
    standing = await delegate_registry.standing(delegate, notable=notable)
    role = await guild_roles.resolve_role(ctx.guild, delegate.guild_tag)
    slots = await GuildContact.filter(delegate_id=delegate.id).values_list(
        "role", flat=True
    )

    me = ctx.guild.me
    if role is None:
        role_line = (
            f"**guild role:** none resolved for `{delegate.guild_tag}` — nothing "
            "to add or remove, which is itself worth knowing"
        )
    else:
        manageable = role.position < me.top_role.position
        role_line = (
            f"**guild role:** {role.mention} (`{role.id}`, position {role.position}; "
            f"mine is {me.top_role.position}) — "
            f"{'holds it' if _holds(member, role) else 'not wearing it'}, "
            f"{'I can manage it' if manageable else '**above me, so I cannot touch it**'}"
        )

    await ctx.reply(
        "\n".join(
            [
                f"**{member}** ({member.id})",
                f"**represents:** `{delegate.guild_tag}` "
                f"({'notable' if notable else 'not notable'})",
                f"**watch last saw:** `{delegate.current_guild_tag or 'no guild'}`",
                f"**forced to:** `{forced}`" if forced else "**forced to:** nothing",
                f"**standing:** `{standing}`",
                role_line,
                f"**contact slots:** {', '.join(sorted(slots)) or 'none'}",
                f"**nickname:** `{member.nick or '(unset)'}`",
            ]
        )
    )
