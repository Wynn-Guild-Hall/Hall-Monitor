"""Shared reading of ``NotabilityCache`` for the notability scripts.

Underscore-prefixed, so the loader treats it as a helper rather than
exposing it as ``~script _signal_rows``.
"""

import json
from dataclasses import dataclass

from hall_monitor.db.models import NotabilityCache

# Canonical column order. Anything the cache carries that isn't here still
# shows up, appended — a signal added later shouldn't vanish from reports.
SIGNALS = (
    "top25_average_online",
    "level_100_plus",
    "season_placement",
    "territory_ownership",
    "war_count",
    "force_override",
)

# Column headings short enough to line up 6 of them in a Discord message.
ABBREVIATIONS = {
    "top25_average_online": "online",
    "level_100_plus": "level",
    "season_placement": "season",
    "territory_ownership": "terr",
    "war_count": "wars",
    "force_override": "forced",
}

DISCORD_LIMIT = 2000


@dataclass(frozen=True)
class Row:
    tag: str
    notable: bool
    signals: dict

    @property
    def met(self) -> set[str]:
        return {name for name, value in self.signals.items() if value is True}

    @property
    def skipped(self) -> set[str]:
        """Signals recorded as ``null`` — evaluated for nothing, not unmet."""
        return {name for name, value in self.signals.items() if value is None}


async def load() -> list[Row]:
    rows = []
    for row in await NotabilityCache.all().order_by("guild_tag"):
        try:
            signals = json.loads(row.signals_json)
        except (TypeError, ValueError):
            signals = {}
        rows.append(
            Row(
                tag=row.guild_tag,
                notable=row.is_notable,
                signals=signals if isinstance(signals, dict) else {},
            )
        )
    return rows


def columns(rows: list[Row]) -> list[str]:
    seen: set[str] = set()
    for row in rows:
        seen |= set(row.signals)
    extra = sorted(name for name in seen if name not in SIGNALS)
    return [*SIGNALS, *extra]


def cell(value) -> str:
    """``Y`` met, ``·`` not met, ``?`` never evaluated."""
    if value is True:
        return "Y"
    return "·" if value is False else "?"


def chunk(lines: list[str], *, prefix: str = "", limit: int = DISCORD_LIMIT) -> list[str]:
    """Split ``lines`` into code blocks that each fit in one message."""
    budget = limit - len(prefix) - len("```\n\n```") - 16
    blocks, current, size = [], [], 0
    for line in lines:
        if current and size + len(line) + 1 > budget:
            blocks.append("```\n" + "\n".join(current) + "\n```")
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        blocks.append("```\n" + "\n".join(current) + "\n```")
    return blocks or ["```\n(none)\n```"]
