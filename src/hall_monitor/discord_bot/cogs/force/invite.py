"""``~force invite`` — hand somebody a representative invite by hand.

The join flow asks Wynncraft whether you're a chief or owner, and that's
right almost always. This is the almost: a guild's events organiser who
holds Strategist, a co-leader whose rank never got updated, somebody
running housing for a guild whose chiefs are all asleep. They speak for
the guild in every sense the Hall cares about and in none that the API
can see.

    ~force invite wisteriablossoms Crrs events

**Monitor-only, and deliberately the exception to `~force rep`'s rule.**
That command refuses to take the operator's word about chief-hood,
because a monitor certain and wrong there produces a representative the
reconcile can't explain. This one *is* the operator's word, on purpose —
so it's a tier higher, logged at warning level, and its reply says out
loud what the bot didn't check.

What is **not** bypassed is everything after the invite. They join
through the ordinary listener, get the ordinary roles, claim the
ordinary contact slots, and are settled by the ordinary hourly pass. Two
consequences worth knowing, both correct:

- If they really are *in* that guild, the guild watch confirms it and
  they stay a Delegate. A rank of Strategist is invisible to the watch,
  which only reads the prefix.
- If they are **not** in that guild, the watch will notice within the
  hour and relegate them to External. This command asserts who speaks
  for a guild; it can't assert membership Wynncraft disagrees with, and
  it doesn't try.

The invite lives a **week** — it's pasted into a DM and waited on
(``discord_invites.HANDED_INVITE_MAX_AGE_SECONDS``), the same as an
observer's and for the same reason.
"""

import logging

from discord.ext import commands

from hall_monitor import external
from hall_monitor.config import settings
from hall_monitor.db.models import PendingInvite
from hall_monitor.discord_bot.cogs.force.assign import Rejected, parse_contact_type
from hall_monitor.discord_bot.permissions import is_monitor
from hall_monitor.external import resolve_profile
from hall_monitor.services import contacts, discord_invites, expel, role_bits

logger = logging.getLogger(__name__)


def parse_roles(names: tuple[str, ...]) -> set[str]:
    """Normalise the requested contact roles, or reject with the valid set.

    An empty set is allowed and means the delegate role alone — the same
    thing `HALL00` means from Minecraft. Rare, but it's a real answer for
    somebody who should be in the room without holding a slot.
    """
    return {parse_contact_type(name) for name in names}


async def hand_out(
    ctx: commands.Context, username: str, guild_tag: str, roles: tuple[str, ...]
) -> str:
    """Mint a representative invite and return what to say about it."""
    if ctx.guild is None:
        return "run this in the server — I need a channel to invite to."
    channel = ctx.guild.get_channel(settings.welcome_channel_id)
    if channel is None:
        return "I can't reach the welcome channel, so I can't mint an invite."

    try:
        wanted = parse_roles(roles)
    except Rejected as exc:
        return str(exc)

    if await expel.is_banned(guild_tag):
        return f"`{guild_tag}` is barred from the Hall."

    # The one thing worth checking, since nothing else will: a typo'd tag
    # would mint a real invite for a guild that doesn't exist, and the
    # first sign of it would be a role and a roster entry nobody ordered.
    name = await external.guild_name_for(guild_tag)
    if name is None:
        return (
            f"Wynncraft doesn't know a guild with the tag `{guild_tag}`. "
            "Check the spelling — this is the one thing I *can* verify here."
        )

    profile = await resolve_profile(username, urgent=True)
    if profile is None:
        return f"`{username}` isn't a Minecraft account I could find."

    try:
        pending = await discord_invites.mint_invite(
            profile.uuid,
            guild_tag,
            role_bits.encode(wanted),
            channel=channel,
            bot=ctx.bot,
            discord_guild=ctx.guild,
            mc_username=profile.username,
            max_age=discord_invites.HANDED_INVITE_MAX_AGE_SECONDS,
            invited_by=getattr(ctx.author, "id", None),
        )
    except discord_invites.AlreadyLiveDelegate:
        return (
            f"`{profile.username}` is already a representative in the Hall. "
            "`~force rep` re-points them; `~force assign` moves their slots."
        )
    except discord_invites.AlreadyObserving as exc:
        return (
            f"`{profile.username}` is already here as an observer (<@"
            f"{exc.discord_user_id}>), so an invite would do nothing when "
            "clicked. `~force rep` promotes an observer — though it does "
            "check chief-hood, which this command exists to skip, so this "
            "one may need doing by hand."
        )

    logger.warning(
        "invite: %s (%s) hand-minted a %s invite for %s with roles %s — "
        "no chief check",
        ctx.author,
        getattr(ctx.author, "id", None),
        guild_tag,
        profile.username,
        ", ".join(sorted(wanted)) or "none",
    )
    return _report(profile.username, guild_tag, name, wanted, pending)


def _report(
    username: str,
    guild_tag: str,
    guild_name: str,
    wanted: set[str],
    pending: PendingInvite,
) -> str:
    held = (
        "`" + "`, `".join(sorted(wanted)) + "`"
        if wanted
        else "no contact slots — the delegate role alone"
    )
    return "\n".join(
        [
            f"Invite for `{username}` as a **{guild_name}** (`{guild_tag}`) "
            f"representative: https://discord.gg/{pending.discord_invite_code}",
            f"Single use, good for a week. On joining they'll take {held}, "
            "displacing whoever holds them now.",
            "",
            "I did **not** check whether they're a chief — that's the point "
            f"of this command. If they aren't in `{guild_tag}` at all, the "
            "hourly guild watch will spot it and relegate them to External.",
        ]
    )


async def take_back(ctx: commands.Context, username: str) -> str:
    """Cancel a representative invite that hasn't been used yet."""
    profile = await resolve_profile(username, urgent=True)
    if profile is None:
        return f"`{username}` isn't a Minecraft account I could find."

    row = await PendingInvite.get_or_none(mc_uuid=profile.uuid)
    if row is None:
        return f"there's no outstanding invite for `{profile.username}`."
    if role_bits.is_observer(row.roles_bits):
        # Mirrors `~unforce observer`'s refusal in the other direction:
        # each command cancels the kind of invite it mints, so a typo
        # can't quietly cancel the other kind.
        return (
            f"`{profile.username}` has an *observer* invite outstanding, not "
            "a representative one — `~unforce observer` cancels that."
        )

    await discord_invites.revoke_invite(row.discord_invite_code, bot=ctx.bot)
    await row.delete()
    logger.warning(
        "invite: %s (%s) revoked the %s invite for %s",
        ctx.author,
        getattr(ctx.author, "id", None),
        row.guild_tag,
        profile.username,
    )
    return (
        f"revoked the `{row.guild_tag}` invite for `{profile.username}`. "
        "If they'd already used it, this changes nothing — `~force expel` or "
        "a kick is what removes somebody who's in."
    )


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="invite")
    @is_monitor()
    async def force_invite(
        ctx: commands.Context, username: str, guild_tag: str, *roles: str
    ) -> None:
        """invite somebody as a guild's representative, skipping the chief check"""
        await ctx.reply(await hand_out(ctx, username, guild_tag, roles))

    @cog.unforce.command(name="invite")
    @is_monitor()
    async def unforce_invite(ctx: commands.Context, username: str) -> None:
        """cancel a representative invite that hasn't been used"""
        await ctx.reply(await take_back(ctx, username))
