"""Guild colour lookup from the Athena guild list, plus Discord-visible contrast.

Athena's raw colours can be very dark or very light; those wash out on
one of Discord's two themes. :func:`to_discord_visible` clamps luminance
into a middle band and floors saturation so the colour reads on both.
"""

import colorsys

from hall_monitor.external import athena

_DEFAULT_COLOUR = "#7289DA"  # blurple; used when a guild has no Athena entry
_MIN_LIGHTNESS = 0.40
_MAX_LIGHTNESS = 0.70
_MIN_SATURATION = 0.55


async def lookup(guild_tag: str, *, urgent: bool = False) -> str | None:
    """Athena's colour for ``guild_tag`` (matched case-insensitively), or
    ``None`` if Athena doesn't know the guild or the row has no colour."""
    tag_lower = guild_tag.lower()
    for row in await athena.guild_list(urgent=urgent):
        if row.prefix.lower() == tag_lower:
            return row.colour
    return None


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
