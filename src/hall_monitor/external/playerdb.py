"""PlayerDB fallback for username→UUID resolution when Mojang ratelimits."""

import httpx

from hall_monitor.config import settings

from ._client import Requester
from ._uuid import dashed
from .mojang import Profile

_requester = Requester(base_url=settings.playerdb_api_base)


async def resolve_profile(
    username: str, *, urgent: bool = False
) -> Profile | None:
    """PlayerDB profile lookup. Returns ``None`` on 404 or ``success: false``."""
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
    player = payload["data"]["player"]
    return Profile(uuid=dashed(player["raw_id"]), username=player["username"])


async def username_to_uuid(username: str, *, urgent: bool = False) -> str | None:
    profile = await resolve_profile(username, urgent=urgent)
    return profile.uuid if profile else None
