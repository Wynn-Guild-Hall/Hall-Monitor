"""PlayerDB fallback for username→UUID resolution when Mojang ratelimits."""

import httpx

from hall_monitor.config import settings

from ._client import Requester

_requester = Requester(base_url=settings.playerdb_api_base)


async def username_to_uuid(username: str, *, urgent: bool = False) -> str | None:
    """Returns the UUID (unhyphenated hex) for ``username``, or ``None`` if
    PlayerDB says the player doesn't exist. Raises
    :class:`httpx.HTTPStatusError` on other non-2xx responses.
    """
    try:
        response = await _requester.get(
            f"/api/player/minecraft/{username}", urgent=urgent
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    payload = response.json()
    if not payload.get("success"):
        return None
    return payload["data"]["player"]["raw_id"]
