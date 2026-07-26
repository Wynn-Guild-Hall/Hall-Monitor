"""Mojang API client — resolves Minecraft usernames to UUIDs.

Preferred over :mod:`playerdb`; the caller in :mod:`external` falls back
to PlayerDB when Mojang ratelimits us.
"""

import httpx

from hall_monitor.config import settings

from ._client import Requester

_requester = Requester(base_url=settings.mojang_api_base)


async def username_to_uuid(username: str, *, urgent: bool = False) -> str | None:
    """Returns the UUID (unhyphenated hex) for ``username``, or ``None`` if
    Mojang doesn't recognise it. Raises :class:`httpx.HTTPStatusError` on
    other non-2xx responses (including 429) so the caller can fall back.
    """
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
    return response.json()["id"]
