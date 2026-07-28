"""Reading and writing a guild's dashboard values.

Storage for the keys declared in ``services/dash_schema.py``. Kept apart
from the schema so the declaration stays pure and testable without a
database, and apart from the cog so the eventual Hallway route can read
these without importing Discord.

Values live in ``DashKV`` as JSON, one row per ``(guild_tag, key)``.
**Unset is the absence of a row**, not a stored null — so a guild that
has never answered and one that answered and then cleared it read
identically, which is what the page wants and what ``~dash unset``
promises.

Rows whose key is no longer declared are ignored on read rather than
deleted. Retiring a key is then reversible: put it back and the guilds
that had answered still have their answers.
"""

import json
import logging

from hall_monitor.db.models import DashKV
from hall_monitor.services import dash_schema

logger = logging.getLogger(__name__)


async def values_for(guild_tag: str) -> dict[str, object]:
    """Every declared key this guild has answered, decoded.

    Keyed by the declared name rather than the stored spelling, so a
    caller can look up what it asked for. Undeclared and unreadable rows
    are skipped — a page should render a guild that has one bad row, not
    fail.
    """
    values: dict[str, object] = {}
    for row in await DashKV.filter(guild_tag__iexact=guild_tag):
        try:
            key = dash_schema.get(row.key)
        except dash_schema.UnknownKey:
            continue  # retired key; left in place, see the module docstring
        try:
            values[key.name] = json.loads(row.value_json)
        except ValueError:
            logger.warning(
                "dash: %s/%s holds unreadable JSON; treating it as unset",
                guild_tag,
                row.key,
            )
    return values


async def write(guild_tag: str, key: dash_schema.Key, value: object) -> None:
    """Store one value, replacing whatever was there."""
    await DashKV.update_or_create(
        guild_tag=guild_tag,
        key=key.name,
        defaults={"value_json": json.dumps(value)},
    )


async def clear(guild_tag: str, key: dash_schema.Key) -> bool:
    """Drop the row. Returns whether there was one — the caller says so.

    A no-op that announces itself is a bug report; one that doesn't is a
    support ticket a week later (DESIGN.md §12.3).
    """
    return bool(await DashKV.filter(guild_tag__iexact=guild_tag, key=key.name).delete())
