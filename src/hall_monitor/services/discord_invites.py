"""Single-use Discord invite lifecycle.

Enforces the ``PendingInvite`` invariants:

- **One row per MC UUID.** Re-requesting revokes the prior Discord
  invite and mints a fresh one.
- **Zero rows if the UUID already has a live Delegate.** MC-time verify
  raises :class:`AlreadyLiveDelegate` so the caller can return a
  "you're already in" kick message.
- **Expiry sweep.** ``sweep_expired`` deletes rows older than
  :attr:`settings.pending_invite_ttl_minutes` and revokes their Discord
  invites — belt-and-braces against the bot going down mid-flow.

It also owns the in-memory invite-usage snapshot that lets
``on_member_join`` work out *which* invite a joining member came through:
Discord tells us someone joined, never how. The snapshot is a
``{code: uses}`` map re-read from the guild on startup and diffed on every
join; see :func:`resolve_used_invite` for the matching rules.

Discord's HTTP surface is passed in as ``channel`` / ``bot`` / ``guild``
so this module stays framework-thin and testable with plain mocks.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord

from hall_monitor.config import settings
from hall_monitor.db.models import Observer, PendingInvite
from hall_monitor.services import delegate_registry, role_bits

logger = logging.getLogger(__name__)

INVITE_MAX_AGE_SECONDS = 600

# An invite a janitor mints and then *hands to a person* — pasted into a
# DM, waited on. Ten minutes is right for a code typed in-game by
# somebody already at the keyboard and useless for somebody who might be
# asleep, so these get a week. The row carries its own ``expires_at`` to
# match (see :class:`PendingInvite`).
#
# Named for how it travels rather than for who gets it: `~force observer`
# and `~force invite` both mint one, and they have nothing else in
# common.
HANDED_INVITE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

# A join can only have consumed an invite that was still alive, so a
# PendingInvite older than the invite's own max_age can't be the match.
# The grace absorbs clock skew between our DB and Discord's expiry.
INVITE_USE_GRACE_SECONDS = 60


class AlreadyLiveDelegate(Exception):
    """Signals that mint_invite was called for a MC UUID whose Discord
    user is already in the server as a delegate."""


class AlreadyObserving(Exception):
    """The UUID belongs to an observer who is already in the server.

    Distinct from :class:`AlreadyLiveDelegate` because the answer is
    different: a live delegate is *done*, whereas an observer has just
    become a chief and is trying to do something perfectly reasonable
    that the invite flow cannot deliver. Minting for them would produce a
    link that does nothing when clicked — an existing member joining
    fires no ``GUILD_MEMBER_ADD``, so the ``PendingInvite`` is never
    consumed — and they'd be left staring at a code that silently did
    nothing. See ``~force rep`` (DESIGN.md §18.1).
    """

    def __init__(self, discord_user_id: int) -> None:
        super().__init__(discord_user_id)
        self.discord_user_id = discord_user_id


# --------------------------------------------------------------------------
# Invite-usage snapshot
# --------------------------------------------------------------------------

_invite_uses: dict[str, int] = {}
_snapshot_lock = asyncio.Lock()


async def refresh_snapshot(guild: discord.Guild) -> dict[str, int]:
    """Re-read ``guild``'s invite list into the snapshot and return a copy.

    Called on bot startup (and on every reconnect — it's idempotent).
    Needs **Manage Server**; without it Discord returns 403 and joins stop
    resolving, so the caller is expected to log loudly.
    """
    fresh = await _fetch_uses(guild)
    _invite_uses.clear()
    _invite_uses.update(fresh)
    return dict(_invite_uses)


def note_minted(code: str) -> None:
    """Track a freshly-minted invite at zero uses.

    Every code we mint lands here rather than waiting for the next full
    read: an invite consumed before then would never appear in a snapshot
    at all, leaving its disappearance with nothing to register against.
    Zero is asserted rather than read off the new invite — Discord's
    create response isn't documented to carry usage metadata.
    """
    _invite_uses[code] = 0


def snapshot() -> dict[str, int]:
    """Copy of the current snapshot — for tests and debug scripts."""
    return dict(_invite_uses)


def reset_snapshot() -> None:
    """Drop everything we know about live invites. Tests use this to keep
    module state from leaking between cases."""
    _invite_uses.clear()


async def _fetch_uses(guild: discord.Guild) -> dict[str, int]:
    """``{code: uses}`` for every invite Discord lists, minus the vanity URL.

    The vanity code's use count only ever climbs, so leaving it in would
    flag it as consumed on every single join. ``uses`` is ``None`` unless
    the bot has Manage Server (Discord withholds invite metadata below
    that) — the code itself is still listed, so vanish-detection survives
    the downgrade even though use-counting doesn't.
    """
    vanity = guild.vanity_url_code
    return {
        invite.code: invite.uses or 0
        for invite in await guild.invites()
        if invite.code != vanity
    }


async def mint_invite(
    mc_uuid: str,
    guild_tag: str,
    roles_bits: int,
    *,
    channel: discord.abc.GuildChannel,
    bot: discord.Client | None = None,
    discord_guild: discord.Guild | None = None,
    mc_username: str | None = None,
    max_age: int = INVITE_MAX_AGE_SECONDS,
    invited_by: int | None = None,
) -> PendingInvite:
    """Mint a single-use Discord invite bound to a UUID and return the row.

    ``bot`` is optional; without it we can still create the new invite
    but can't ask Discord to delete a prior one — the sweep will collect
    it after TTL. Tests exercise both paths.

    A non-default ``max_age`` also writes ``expires_at``, so the sweep and
    the used-invite matcher agree with Discord about how long this one
    lives. The default leaves it NULL and everything behaves as before.
    """
    if await delegate_registry.is_current_member(mc_uuid, discord_guild=discord_guild):
        raise AlreadyLiveDelegate(mc_uuid)

    # An observer is already in the server, so an invite is exactly the
    # wrong answer for them — see `AlreadyObserving`. Skipped when the
    # caller is `~force observer` itself, which is minting *for* them.
    if roles_bits != role_bits.OBSERVER:
        observing = await Observer.get_or_none(mc_uuid=mc_uuid)
        if observing is not None and (
            discord_guild is None
            or discord_guild.get_member(observing.discord_user_id) is not None
        ):
            raise AlreadyObserving(observing.discord_user_id)

    prior = await PendingInvite.get_or_none(mc_uuid=mc_uuid)
    if prior is not None:
        if bot is not None:
            await revoke_invite(prior.discord_invite_code, bot=bot)
        await prior.delete()

    invite = await channel.create_invite(
        max_uses=1,
        max_age=max_age,
        reason=f"hall-monitor: {guild_tag} verification for {mc_uuid}",
    )
    note_minted(invite.code)
    return await PendingInvite.create(
        mc_uuid=mc_uuid,
        mc_username=mc_username,
        guild_tag=guild_tag,
        roles_bits=roles_bits,
        discord_invite_code=invite.code,
        expires_at=(
            None
            if max_age == INVITE_MAX_AGE_SECONDS
            else datetime.now(timezone.utc) + timedelta(seconds=max_age)
        ),
        invited_by_discord_user_id=invited_by,
    )


async def revoke_invite(code: str, *, bot: discord.Client) -> None:
    """Delete a Discord invite by code. Idempotent — a 404 is fine."""
    try:
        await bot.http.delete_invite(code, reason="hall-monitor: revoke")
    except discord.NotFound:
        pass
    # Only forget the code once Discord agrees it's gone; on any other
    # error the invite may still be live and worth tracking.
    _invite_uses.pop(code, None)


async def sweep_expired(*, bot: discord.Client | None = None) -> int:
    """Delete PendingInvite rows past their TTL and revoke their Discord
    invites. Returns the count deleted for logging.

    ``bot`` is optional so scheduler binding stays flexible; without it
    the DB rows still get cleaned but the Discord-side invites will just
    expire naturally (10-minute ``max_age`` we set on mint)."""
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(minutes=settings.pending_invite_ttl_minutes)
    # A row carrying its own `expires_at` is swept on that instead: an
    # observer invite lives a week, and sweeping it at the default 45
    # minutes would revoke a live Discord invite somebody is still
    # holding, with nothing to say why it stopped working.
    stale = [
        row
        for row in await PendingInvite.all()
        if (row.expires_at is not None and row.expires_at < now)
        or (row.expires_at is None and row.created_at < threshold)
    ]
    for row in stale:
        if bot is not None:
            try:
                await revoke_invite(row.discord_invite_code, bot=bot)
            except Exception:
                logger.exception(
                    "sweep: failed to revoke Discord invite %s", row.discord_invite_code
                )
        await row.delete()
    return len(stale)


async def resolve_used_invite(member: discord.Member) -> PendingInvite | None:
    """Work out which of our invites ``member`` joined through.

    Diffs a fresh read of the guild's invite list against the snapshot.
    Two things mark a code as used:

    - **uses went up** — direct evidence, but only visible for invites
      Discord still lists.
    - **the code vanished** — what actually happens to ours, since a
      ``max_uses=1`` invite is deleted the moment it's consumed. Expiry
      looks identical from here, hence the freshness filter below.

    Candidates are then intersected with our own ``PendingInvite`` rows, so
    joins through anyone else's invite (or the vanity URL) resolve to
    ``None`` rather than to a wrong row. Anything still ambiguous returns
    ``None`` too — a mis-attributed join would bind a stranger's Discord
    account to someone else's Minecraft UUID, which is much worse than
    asking the representative to type their ``HALL<NN>`` code again.
    """
    async with _snapshot_lock:
        before = dict(_invite_uses)
        try:
            fresh = await _fetch_uses(member.guild)
        except discord.Forbidden:
            logger.error(
                "join: can't read the invite list (needs Manage Server); "
                "%s joined unresolved",
                member.id,
            )
            return None
        _invite_uses.clear()
        _invite_uses.update(fresh)

    consumed = [code for code, uses in fresh.items() if uses > before.get(code, 0)]
    vanished = [code for code in before if code not in fresh]

    return await _match_pending(consumed) or await _match_pending(vanished)


async def _match_pending(codes: list[str]) -> PendingInvite | None:
    """The single live ``PendingInvite`` among ``codes``, if there is one.

    Rows past the Discord invite's own ``max_age`` are dropped: their
    invite expired rather than being consumed, and leaving them in would
    make a quiet period's worth of expiries look ambiguous.

    "Its own" is literal — the window is **per row**, not global. An
    observer invite lives a week, and judging it by the MC flow's ten
    minutes would read a perfectly live invite as long expired and refuse
    to resolve the join.
    """
    if not codes:
        return None
    now = datetime.now(timezone.utc)
    grace = timedelta(seconds=INVITE_USE_GRACE_SECONDS)
    default_cutoff = now - timedelta(
        seconds=INVITE_MAX_AGE_SECONDS + INVITE_USE_GRACE_SECONDS
    )
    rows = [
        row
        for row in await PendingInvite.filter(discord_invite_code__in=codes).all()
        if (row.expires_at is not None and row.expires_at + grace >= now)
        or (row.expires_at is None and row.created_at >= default_cutoff)
    ]
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        logger.warning(
            "join: %d pending invites changed at once (%s) — can't tell which "
            "was used; leaving them all for the sweep",
            len(rows),
            ", ".join(row.discord_invite_code for row in rows),
        )
    return None
