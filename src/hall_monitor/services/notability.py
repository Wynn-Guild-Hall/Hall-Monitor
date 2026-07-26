"""Aggregate the guild-notability signals into a single boolean.

Signals: average-activity leaderboard, guild level, season placements,
territory ownership across the current season, plus manual force overrides.
"""


async def is_notable(guild_tag: str) -> bool:
    raise NotImplementedError


async def refresh_all() -> None:
    """Recompute notability for every known guild and refresh the cache."""
    raise NotImplementedError
