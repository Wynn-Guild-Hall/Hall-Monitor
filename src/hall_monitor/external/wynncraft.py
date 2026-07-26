"""Wynncraft v3 API client.

Reads ``WYNNCRAFT_API_TOKEN`` from Settings; when set, sends it as the auth
header. Works either way — the token just moves us off the shared anon
ratelimit onto a private application ratelimit.
"""

import httpx

from hall_monitor.config import settings


def _headers() -> dict[str, str]:
    if settings.wynncraft_api_token:
        return {"Authorization": f"Bearer {settings.wynncraft_api_token}"}
    return {}


async def get_player_guild(uuid: str) -> dict | None:
    async with httpx.AsyncClient(base_url=settings.wynncraft_api_base, timeout=10.0, headers=_headers()) as client:
        response = await client.get(f"/v3/player/{uuid}")
        if response.status_code == 200:
            return response.json().get("guild")
        return None


async def get_seasons() -> dict | None:
    async with httpx.AsyncClient(base_url=settings.wynncraft_api_base, timeout=10.0, headers=_headers()) as client:
        response = await client.get("/v3/guild/seasons")
        if response.status_code == 200:
            return response.json()
        return None
