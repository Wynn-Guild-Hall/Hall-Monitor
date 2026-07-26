"""PlayerDB fallback for username→UUID resolution when Mojang ratelimits."""

import httpx

from hall_monitor.config import settings


async def username_to_uuid(username: str) -> str | None:
    async with httpx.AsyncClient(base_url=settings.playerdb_api_base, timeout=5.0) as client:
        response = await client.get(f"/api/player/minecraft/{username}")
        if response.status_code == 200 and response.json().get("success"):
            return response.json()["data"]["player"]["raw_id"]
        return None
