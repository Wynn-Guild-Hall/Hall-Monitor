"""Single-use Discord invite lifecycle.

Enforces the ``PendingInvite`` invariants: one row per MC UUID, none if the
UUID is already a live delegate, and a background sweep for expiry.
"""


async def mint_invite(mc_uuid: str, guild_tag: str, roles_bits: int) -> str:
    """Mint a single-use Discord invite bound to a UUID and return its URL."""
    raise NotImplementedError


async def revoke_invite(discord_invite_code: str) -> None:
    """Revoke an outstanding Discord invite by code."""
    raise NotImplementedError


async def sweep_expired() -> int:
    """Delete stale ``PendingInvite`` rows and revoke their Discord invites."""
    raise NotImplementedError


async def resolve_used_invite(member) -> None:
    """Match a joining member to their ``PendingInvite`` and apply bound roles."""
    raise NotImplementedError
