"""Mojang API client — resolves Minecraft usernames to UUIDs."""

import httpx

from hall_monitor.config import settings


async def username_to_uuid(username: str) -> str | None:
    async with httpx.AsyncClient(base_url=settings.mojang_api_base, timeout=5.0) as client:
        response = await client.get(f"/users/profiles/minecraft/{username}")
        if response.status_code == 200:
            return response.json()["id"]
        return None
