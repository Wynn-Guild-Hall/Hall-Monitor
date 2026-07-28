"""``~force assign`` — hand a guild's contact slot to a delegate.

Two tiers call this and they scope differently. A janitor (or monitor)
assigns for *the target's* guild, whichever that is. An ownership contact
assigns only within their own guild — the command is how a guild's
leadership rearranges its own representatives, not a way to reach into
someone else's.

Either way the target has to already be a registered delegate: the
contact slot is a foreign key onto their row, and there's no meaningful
"contact for VETS" who never verified as a VETS representative.
"""

from dataclasses import dataclass

import discord
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.db.models import Delegate
from hall_monitor.discord_bot.permissions import has_any_role, is_ownership_contact
from hall_monitor.services import contacts, delegate_registry, guild_tag as guild_tag_service


class Rejected(Exception):
    """Carries the user-facing reason a ``~force assign`` can't proceed."""


@dataclass(frozen=True)
class Target:
    """The guild slot a call resolved to, and who it's being handed to."""

    guild_tag: str
    delegate: Delegate


def parse_contact_type(contact_type: str) -> str:
    """Normalise a typed role name, or reject it with the valid set."""
    role = contact_type.strip().lower().removesuffix("contact").strip("_- ")
    if role not in contacts.CONTACT_ROLES:
        raise Rejected(
            f"`{contact_type}` isn't a contact role — pick one of "
            + ", ".join(f"`{name}`" for name in contacts.CONTACT_ROLES)
            + "."
        )
    return role


def resolve_guild_tag(
    caller_tag: str | None, target_tag: str | None, *, caller_is_janitor: bool
) -> str:
    """Which guild's slot this caller may touch for this target.

    Pure so the scoping rules are unit-testable without a Discord context;
    raises :class:`Rejected` with the message the caller should see.
    """
    if target_tag is None:
        raise Rejected(
            "they aren't a registered delegate — they need to verify through "
            "the join flow first."
        )
    if caller_is_janitor:
        return target_tag
    if caller_tag is None:
        raise Rejected(
            "I don't have you on file as a delegate, so I can't tell which "
            "guild you're assigning for."
        )
    if not guild_tag_service.matches(caller_tag, target_tag):
        raise Rejected(
            f"ownership contacts can only assign within their own guild — "
            f"they represent `{target_tag}`, you represent `{caller_tag}`."
        )
    return caller_tag


async def resolve_target(ctx: commands.Context, user: discord.Member) -> Target:
    caller = await delegate_registry.get_by_discord_user_id(ctx.author.id)
    target = await delegate_registry.get_by_discord_user_id(user.id)
    guild_tag = resolve_guild_tag(
        caller.guild_tag if caller else None,
        target.guild_tag if target else None,
        caller_is_janitor=has_any_role(
            ctx, settings.janitor_role_id, settings.monitor_role_id
        ),
    )
    return Target(guild_tag=guild_tag, delegate=target)


def displacement_note(displaced: contacts.Displacement | None) -> str:
    if displaced is None:
        return ""
    who = f"<@{displaced.delegate.discord_user_id}>"
    if displaced.kicked:
        return f" {who} lost the role and was kicked — it was their last one."
    return f" {who} lost the role."


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="assign")
    @is_ownership_contact()  # nests: janitors and monitors pass too
    async def force_assign(
        ctx: commands.Context, user: discord.Member, contact_type: str
    ) -> None:
        """hand a guild's contact slot to a delegate"""
        try:
            role = parse_contact_type(contact_type)
            target = await resolve_target(ctx, user)
        except Rejected as exc:
            await ctx.reply(str(exc))
            return

        try:
            displaced = await contacts.assign_contact(
                target.guild_tag,
                role,
                target.delegate,
                discord_guild=ctx.guild,
                reason=f"hall-monitor: ~force assign by {ctx.author}",
            )
        except contacts.ExternalDelegate as exc:
            # Granting it anyway would look like it worked until the next
            # reconcile stripped it — which is exactly how this was found.
            where = f"`{exc.playing_for}`" if exc.playing_for else "another guild"
            await ctx.reply(
                f"{user.mention} is playing for {where}, so they can't be a "
                f"`{target.guild_tag}` contact. If that's wrong, "
                f"`~force guild {user.mention} {target.guild_tag} 3mo` first."
            )
            return
        await ctx.reply(
            f"{user.mention} is now the `{role}` contact for "
            f"`{target.guild_tag}`.{displacement_note(displaced)}"
        )

    @cog.unforce.command(name="assign")
    @is_ownership_contact()  # nests: janitors and monitors pass too
    async def unforce_assign(
        ctx: commands.Context, user: discord.Member, contact_type: str
    ) -> None:
        """leave a guild's contact slot unclaimed"""
        try:
            role = parse_contact_type(contact_type)
            target = await resolve_target(ctx, user)
        except Rejected as exc:
            await ctx.reply(str(exc))
            return

        cleared = await contacts.clear_contact(
            target.guild_tag,
            role,
            delegate=target.delegate,
            discord_guild=ctx.guild,
            reason=f"hall-monitor: ~unforce assign by {ctx.author}",
        )
        if cleared is None:
            await ctx.reply(
                f"{user.mention} isn't the `{role}` contact for "
                f"`{target.guild_tag}` — nothing to clear."
            )
            return
        tail = (
            " They were kicked — it was their last contact role."
            if cleared.kicked
            else ""
        )
        await ctx.reply(
            f"`{target.guild_tag}`'s `{role}` contact is unclaimed again.{tail}"
        )
