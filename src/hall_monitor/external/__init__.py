"""Third-party API clients + orchestrators that live above them."""

import httpx

from . import mojang, playerdb


async def resolve_username_to_uuid(
    username: str, *, urgent: bool = False
) -> str | None:
    """Try Mojang first; fall back to PlayerDB on 429 or connection error.

    A 404 from Mojang is authoritative — the username genuinely doesn't
    exist, so we don't ask PlayerDB. Other HTTP errors (5xx, etc.) also
    fall through to PlayerDB so a Mojang outage doesn't block a join.
    """
    try:
        return await mojang.username_to_uuid(username, urgent=urgent)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        # 429, 5xx, etc. — try the fallback.
    except httpx.RequestError:
        pass
    return await playerdb.username_to_uuid(username, urgent=urgent)
