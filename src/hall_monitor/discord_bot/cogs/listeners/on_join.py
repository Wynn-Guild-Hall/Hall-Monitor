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
them, and ``services/guild_roles.py`` for the guild colour that follows.
The nickname is written after all of it: a join is the one time the
visible part is ours to pick rather than preserve.
"""

import logging

import discord
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.db.models import Observer
from hall_monitor.services import (
    contacts,
    delegate_registry,
    discord_invites,
    expel,
    guild_roles,
    nicknames,
    role_bits,
    roster,
)

logger = logging.getLogger(__name__)


class RoleResolutionError(RuntimeError):
    """A role we need is unconfigured, or configured but absent from the guild."""


async def _welcome_observer(member: discord.Member, pending) -> None:
    """Give an observer their one role and write no ``Delegate`` row.

    The absent row is load-bearing rather than an omission: it's what
    keeps them out of the guild watch, the reconcile, the roster and the
    nickname enforcer without any of those learning what an observer is
    (DESIGN.md §18).

    A failed role application leaves the ``PendingInvite`` for the sweep,
    exactly as the representative path does — except that here they're
    already *in* the server, so the janitor is told rather than left to
    notice. There's no second invite to redeem.
    """
    role = (
        member.guild.get_role(settings.observer_role_id)
        if settings.observer_role_id
        else None
    )
    if role is None:
        logger.error(
            "join: %s arrived on an observer invite but the observer role "
            "(%s) is unset or missing; they're in the server with nothing",
            member.id,
            settings.observer_role_id,
        )
        return

    try:
        await member.add_roles(role, reason="hall-monitor: observer invite")
    except discord.HTTPException:
        logger.exception(
            "join: couldn't give %s the observer role; leaving the pending "
            "invite for the sweep",
            member.id,
        )
        return

    # The binding has to outlive the invite. Throwing it away here left
    # the bot unable to say who an observer is — and, worse, unable to
    # tell that one who later becomes a chief is *already in the room*,
    # so it minted them an invite that does nothing when clicked. See
    # `db.models.Observer`.
    await Observer.update_or_create(
        mc_uuid=pending.mc_uuid,
        defaults={
            "mc_username": pending.mc_username,
            "discord_user_id": member.id,
            "invited_by_discord_user_id": pending.invited_by_discord_user_id,
        },
    )
    await pending.delete()
    logger.info("join: %s is now an observer (%s)", member.id, pending.mc_username)


async def _turn_away(member: discord.Member, pending) -> None:
    """Remove someone who arrived on an invite their guild has since lost.

    The ``PendingInvite`` goes with them rather than being left for the
    sweep: unlike a failed role application, this isn't a state a retry
    could improve, and leaving the row would have the sweep revoke an
    invite that has already been consumed.
    """
    await pending.delete()
    try:
        await member.kick(
            reason=f"hall-monitor: {pending.guild_tag} is expelled from the Guild Hall"
        )
    except discord.HTTPException:
        logger.exception(
            "join: %s joined for expelled guild %s and couldn't be removed",
            member.id,
            pending.guild_tag,
        )
        return
    logger.warning(
        "join: %s arrived on a %s invite minted before the expulsion; removed",
        member.id,
        pending.guild_tag,
    )


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

        # Observers first, and before the ban check deliberately: they
        # represent nobody, so their `guild_tag` is the reserved `NONE`
        # and asking whether it's expelled is a question about nothing.
        # Left in the other order, a stray `~force expel NONE` would
        # silently turn away every observer for a reason nobody could
        # find.
        #
        # Almost none of what follows applies to them either: no roles to
        # decode, no guild role, no contact slots, no `Delegate` row and
        # therefore no nickname tag. Branching here rather than threading
        # a flag through each step is what keeps every later pass free of
        # "unless they're an observer".
        if role_bits.is_observer(pending.roles_bits):
            await _welcome_observer(member, pending)
            return

        # An invite lives ten minutes, so one minted just before the Hall
        # voted the guild out is still redeemable. The verify route can't
        # know that yet and the ban has to be checked again here, at the
        # only other point where a banned guild gets through the door.
        if await expel.is_banned(pending.guild_tag):
            await _turn_away(member, pending)
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

        # The guild's aesthetic role rides along in the same `add_roles`
        # call rather than costing a second one. It's cosmetic, so a role
        # that can't be created doesn't stop the verification — unlike the
        # delegate and contact roles above, which are the verification.
        guild_role = await guild_roles.ensure_guild_role(
            member.guild, pending.guild_tag
        )
        if guild_role is not None:
            roles.append(guild_role)

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
            pending.mc_uuid, member.id, pending.guild_tag, pending.mc_username
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
        try:
            displaced = await contacts.resolve_conflicts_and_kick_if_empty(
                guild_tag,
                role_bits.decode(roles_bits),
                delegate,
                discord_guild=member.guild,
                grant=False,  # add_roles above already applied the whole set
                reason=f"hall-monitor: {guild_tag} contact reassigned on verification",
            )
        except contacts.NotTheirGuild as exc:
            # Only reachable with a `~force guild` override written before
            # they verified — the registration seeds them into their own
            # guild. They're a delegate either way; the slots wait.
            logger.warning(
                "join: %s verified for %s but represents %s, so no contact "
                "slots were claimed",
                member.id,
                guild_tag,
                exc.represents,
            )
            displaced = []
        for loss in displaced:
            logger.info(
                "join: %s took the %s %s slot from %s (kicked=%s)",
                member.id,
                guild_tag,
                loss.role,
                loss.delegate.discord_user_id,
                loss.kicked,
            )

        # Last, and separately: a join is the one time we pick the visible
        # part of a nickname rather than preserving one, and it needs the
        # Delegate row above to know which tag to write.
        await nicknames.enforce(
            member, reason=f"hall-monitor: {guild_tag} delegate verification"
        )

        # The roster now names a contact it didn't before. Debounced, so a
        # verification that claims four slots redraws the channel once.
        roster.request_sync(member.guild)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OnJoin(bot))
