"""Resolve which single-use invite a new member joined through, then apply their bound roles.

The invite is the only thing linking a Discord account to the Minecraft
UUID that requested it, so a member we can't match gets nothing — no
roles, no ``Delegate`` row — and their ``PendingInvite`` stays put for the
sweep. Same for a role application that fails part-way: a delegate
without roles is worse than one who has to type their code again,
and a lingering ``Delegate`` row would make ``mint_invite`` refuse the
retry.

Claiming the contact slots comes last, once the delegate row exists —
see ``services/contacts.py`` for what displacing a prior holder costs
them. The guild aesthetic role and nickname enforcement are still absent
here; later stages hang off the same listener.
"""

import logging

import discord
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.services import (
    contacts,
    delegate_registry,
    discord_invites,
    role_bits,
)

logger = logging.getLogger(__name__)


class RoleResolutionError(RuntimeError):
    """A role we need is unconfigured, or configured but absent from the guild."""


def resolve_roles(guild: discord.Guild, roles_bits: int) -> list[discord.Role]:
    """Delegate role plus one contact role per bit set in ``roles_bits``.

    Raises :class:`RoleResolutionError` rather than silently applying a
    partial set — half the roles a representative asked for is a support
    ticket waiting to happen, and the retry path is cheap.
    """
    wanted = {"delegate": settings.delegate_role_id}
    for name in sorted(role_bits.decode(roles_bits)):
        wanted[name] = contacts.contact_role_id(name)

    resolved = []
    for label, role_id in wanted.items():
        if not role_id:
            raise RoleResolutionError(f"{label} role ID is unset")
        role = guild.get_role(role_id)
        if role is None:
            raise RoleResolutionError(f"{label} role {role_id} is not in the guild")
        resolved.append(role)
    return resolved


class OnJoin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Seed the invite snapshot. Re-runs harmlessly on every reconnect."""
        guild = (
            self.bot.get_guild(settings.discord_guild_id)
            if settings.discord_guild_id
            else None
        )
        if guild is None:
            logger.warning(
                "invite snapshot not seeded: guild %s unavailable",
                settings.discord_guild_id,
            )
            return
        try:
            tracked = await discord_invites.refresh_snapshot(guild)
        except discord.HTTPException:
            logger.exception(
                "invite snapshot not seeded: can't read the invite list "
                "(needs Manage Server) — joins won't resolve"
            )
            return
        logger.info("invite snapshot seeded with %d live invites", len(tracked))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if settings.discord_guild_id and member.guild.id != settings.discord_guild_id:
            return
        if member.bot:
            return  # bots are added by OAuth, not through an invite

        pending = await discord_invites.resolve_used_invite(member)
        if pending is None:
            logger.info(
                "join: %s (%s) didn't match a pending invite; no roles applied",
                member,
                member.id,
            )
            return

        try:
            roles = resolve_roles(member.guild, pending.roles_bits)
        except (
            RoleResolutionError,
            role_bits.UnknownRoleBit,
            contacts.UnknownContactRole,
        ):
            logger.exception(
                "join: can't resolve roles for %s (%s / %s); "
                "leaving the pending invite for the sweep",
                member.id,
                pending.mc_uuid,
                pending.guild_tag,
            )
            return

        try:
            await member.add_roles(
                *roles,
                reason=f"hall-monitor: {pending.guild_tag} delegate verification",
            )
        except discord.HTTPException:
            logger.exception(
                "join: failed to apply roles to %s (%s); "
                "leaving the pending invite for the sweep",
                member.id,
                pending.guild_tag,
            )
            return

        delegate = await delegate_registry.register(
            pending.mc_uuid, member.id, pending.guild_tag
        )
        guild_tag, roles_bits = pending.guild_tag, pending.roles_bits
        await pending.delete()
        logger.info(
            "join: %s is now a %s delegate with roles %s",
            member.id,
            guild_tag,
            ", ".join(role.name for role in roles),
        )

        # Claiming the slots is deliberately after the promotion: the
        # member is already a delegate by this point, so a displacement
        # that half-fails is a contact-roster problem, not a reason to
        # unwind a verification that otherwise worked.
        displaced = await contacts.resolve_conflicts_and_kick_if_empty(
            guild_tag,
            role_bits.decode(roles_bits),
            delegate,
            discord_guild=member.guild,
            grant=False,  # add_roles above already applied the whole set
            reason=f"hall-monitor: {guild_tag} contact reassigned on verification",
        )
        for loss in displaced:
            logger.info(
                "join: %s took the %s %s slot from %s (kicked=%s)",
                member.id,
                guild_tag,
                loss.role,
                loss.delegate.discord_user_id,
                loss.kicked,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OnJoin(bot))
