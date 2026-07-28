"""Athena API client — Wynntils' community guild cache.

The one endpoint we consume is ``/cache/get/guildList``, which returns a
row per guild — around 2 700 of them. We read the ``color`` field to seed
the aesthetic Discord role for each major guild (DESIGN.md §11).

Two things about the payload worth not rediscovering: the guild *name*
arrives as ``_id`` (Athena keys its cache by name), and a guild that never
picked a colour has ``color: ""`` rather than a missing key or a null —
roughly one row in six.
"""

from dataclasses import dataclass

from hall_monitor.config import settings

from ._client import Requester


_requester = Requester(base_url=settings.athena_api_base)


@dataclass(frozen=True)
class AthenaGuild:
    name: str
    prefix: str
    colour: str | None  # hex like "#7f2727"; None for a guild with none set


async def guild_list(*, urgent: bool = False) -> tuple[AthenaGuild, ...]:
    """Every guild Athena knows about. One request; caller caches if needed."""
    response = await _requester.get("/cache/get/guildList", urgent=urgent)
    payload = response.json() or []
    return tuple(
        AthenaGuild(
            name=row["_id"],
            prefix=row["prefix"],
            colour=row.get("color") or None,
        )
        for row in payload
    )
