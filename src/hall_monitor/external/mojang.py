"""Mojang API client — resolves Minecraft usernames to UUIDs.

Preferred over :mod:`playerdb`; the caller in :mod:`external` falls back
to PlayerDB when Mojang ratelimits us.
"""

from dataclasses import dataclass

import httpx

from hall_monitor.config import settings

from ._client import Requester
from ._uuid import dashed

_requester = Requester(base_url=settings.mojang_api_base)


@dataclass(frozen=True)
class Profile:
    """A Minecraft profile — canonical username + dashed UUID.

    Mojang returns the UUID bare; we dash it on the way in so one shape
    reaches the database and Wynncraft. See :mod:`._uuid`.
    """

    uuid: str
    username: str


async def resolve_profile(
    username: str, *, urgent: bool = False
) -> Profile | None:
    """Full Mojang profile lookup. Returns ``None`` on 404 / empty body."""
    try:
        response = await _requester.get(
            f"/users/profiles/minecraft/{username}", urgent=urgent
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    if response.status_code == 204 or not response.content:
        return None
    payload = response.json()
    return Profile(uuid=dashed(payload["id"]), username=payload["name"])


async def username_to_uuid(username: str, *, urgent: bool = False) -> str | None:
    """Convenience wrapper around :func:`resolve_profile` for callers who
    only need the UUID."""
    profile = await resolve_profile(username, urgent=urgent)
    return profile.uuid if profile else None
