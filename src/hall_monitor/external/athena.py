"""Athena API client — Wynntils' community guild cache.

The one endpoint we consume is ``/cache/get/guildList``, which returns a
name/prefix/colour row per guild. We read the ``color`` field to seed the
aesthetic Discord role for each notable guild (see Stage 8).
"""

from dataclasses import dataclass

from hall_monitor.config import settings

from ._client import Requester


_requester = Requester(base_url=settings.athena_api_base)


@dataclass(frozen=True)
class AthenaGuild:
    name: str
    prefix: str
    colour: str | None  # hex like "#7f2727"; may be missing for some guilds


async def guild_list(*, urgent: bool = False) -> tuple[AthenaGuild, ...]:
    """Every guild Athena knows about. One request; caller caches if needed."""
    response = await _requester.get("/cache/get/guildList", urgent=urgent)
    payload = response.json() or []
    return tuple(
        AthenaGuild(
            name=row["name"],
            prefix=row["prefix"],
            colour=row.get("color"),
        )
        for row in payload
    )
