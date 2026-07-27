"""Guild colour lookup from the Athena guild list, plus Discord-visible contrast.

Athena's raw colours can be very dark or very light; those wash out on
one of Discord's two themes. :func:`to_discord_visible` clamps luminance
into a middle band and floors saturation so the colour reads on both.

The guild list is one request for every guild Athena knows, so it's held
in memory for an hour rather than fetched per lookup — a guild picking a
new colour is not something anybody needs to see propagate in seconds,
and the alternative is a third-party round trip on the join path.
"""

import colorsys
import logging
import time

import httpx

from hall_monitor.external import athena
from hall_monitor.services import guild_tag as tags

logger = logging.getLogger(__name__)

DEFAULT_COLOUR = "#7289DA"  # blurple; used when a guild has no Athena entry
_MIN_LIGHTNESS = 0.40
_MAX_LIGHTNESS = 0.70
_MIN_SATURATION = 0.55

_LIST_TTL_S = 3600
_list_cache: "tuple[float, tuple[athena.AthenaGuild, ...]] | None" = None

# A malformed payload lands here as readily as a network fault: the rows
# are indexed by key on the way into the dataclass.
_LOOKUP_FAILURES = (httpx.HTTPError, ValueError, KeyError, TypeError)


async def lookup(guild_tag: str, *, urgent: bool = False) -> str | None:
    """Athena's colour for ``guild_tag`` (matched case-insensitively), or
    ``None`` if Athena doesn't know the guild or the row has no colour.

    Raises whatever the fetch raises when there's no cached list to fall
    back on. Callers wanting a colour no matter what want
    :func:`colour_for`.
    """
    for row in await guild_list(urgent=urgent):
        if tags.matches(row.prefix, guild_tag):
            return row.colour
    return None


async def colour_for(guild_tag: str, *, urgent: bool = False) -> str:
    """A Discord-visible colour for ``guild_tag``, always.

    Falls back to :data:`DEFAULT_COLOUR` when Athena doesn't know the
    guild, can't be reached, or hands back something that isn't a hex
    colour. A role in the wrong colour is a cosmetic problem; a
    verification that fails because a third-party cache is down is not.
    """
    try:
        raw = await lookup(guild_tag, urgent=urgent)
    except _LOOKUP_FAILURES:
        logger.warning(
            "athena: colour lookup for %s failed; using the default colour",
            guild_tag,
            exc_info=True,
        )
        return DEFAULT_COLOUR
    if raw is None:
        return DEFAULT_COLOUR
    try:
        return to_discord_visible(raw)
    except ValueError:
        logger.warning(
            "athena: %s has colour %r, which isn't a hex colour; using the default",
            guild_tag,
            raw,
        )
        return DEFAULT_COLOUR


async def guild_list(*, urgent: bool = False) -> tuple[athena.AthenaGuild, ...]:
    """The Athena guild list, refetched at most once per :data:`_LIST_TTL_S`.

    A failed refresh serves the stale copy when we have one — an hour-old
    colour beats no colour, and Athena being briefly down shouldn't make
    every guild role blurple.
    """
    global _list_cache
    if _list_cache is not None:
        loaded_at, rows = _list_cache
        if time.monotonic() - loaded_at < _LIST_TTL_S:
            return rows
    try:
        rows = await athena.guild_list(urgent=urgent)
    except _LOOKUP_FAILURES:
        if _list_cache is None:
            raise
        logger.warning(
            "athena: guild list refresh failed; serving the stale copy",
            exc_info=True,
        )
        return _list_cache[1]
    _list_cache = (time.monotonic(), rows)
    return rows


def reset_cache() -> None:
    """Drop the memoised guild list. Tests use this to keep module state
    from leaking between cases."""
    global _list_cache
    _list_cache = None


def to_discord_visible(hex_colour: str) -> str:
    """Return a Discord-legible variant of ``hex_colour``.

    Clamps HLS lightness into ``[0.40, 0.70]`` (so we're neither near-black
    nor near-white) and floors saturation at ``0.55`` (so we don't render
    as grey mush on either theme). Input can be ``"#RRGGBB"`` or ``"RRGGBB"``.
    """
    r, g, b = _parse_hex(hex_colour)
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    l = min(_MAX_LIGHTNESS, max(_MIN_LIGHTNESS, l))
    # Floor saturation only when the input has a hue to lift — an achromatic
    # grey should stay grey, not get assigned an arbitrary tint.
    if s > 0.05:
        s = max(_MIN_SATURATION, s)
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return "#{:02X}{:02X}{:02X}".format(
        round(r2 * 255), round(g2 * 255), round(b2 * 255)
    )


def _parse_hex(hex_colour: str) -> tuple[int, int, int]:
    stripped = hex_colour.strip().lstrip("#")
    if len(stripped) != 6:
        raise ValueError(f"expected #RRGGBB, got {hex_colour!r}")
    return int(stripped[0:2], 16), int(stripped[2:4], 16), int(stripped[4:6], 16)
