"""Wynnpool API client — leaderboards and season ratings that feed notability."""

import httpx

from hall_monitor.config import settings


async def _get(path: str) -> dict | list | None:
    async with httpx.AsyncClient(base_url=settings.wynnpool_api_base, timeout=10.0) as client:
        response = await client.get(path)
        if response.status_code == 200:
            return response.json()
        return None


async def average_online_leaderboard():
    return await _get("/leaderboard/guild-average-online")


async def guild_level_leaderboard():
    return await _get("/leaderboard/guildLevel")


async def season_rating(season: int):
    return await _get(f"/leaderboard/season-rating/{season}")
