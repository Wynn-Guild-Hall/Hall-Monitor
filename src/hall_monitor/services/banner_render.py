"""Draw a guild's Minecraft banner as a PNG, for use as a Discord emote.

A Wynncraft banner is a base dye colour plus an ordered stack of
patterns, each in a dye colour of its own. Wynnpool publishes the pattern
art as SVG, and — this is the part worth stating, because it's the whole
design — **the art is a mask, not a picture**. Every shape in it is black
with an opacity of 1, 0.5 or 0.25; the colour comes from us, the alpha
from the art, and the partial opacities are Minecraft's own shading.

That's confirmed against Wynnpool's site rather than guessed: it renders
a banner as a coloured `div` per layer with
``mask: url(/banners/<PATTERN>.svg); mask-size: cover`` over a
base-coloured box, which is exactly the composite below.

Two departures from that reference, both deliberate:

- **``SILVER`` is a colour here.** Wynncraft's API says ``SILVER`` where
  Minecraft now says ``LIGHT_GRAY``, and Wynnpool's own lookup is keyed
  only on the modern name — so a silver banner renders with no base
  colour at all on their site. Both names map here.
- **A missing pattern is skipped, not fatal.** Wynnpool has no art for
  several real patterns (``GLOBE``, ``PIGLIN``, ``DIAGONAL_UP_LEFT``,
  ``DIAGONAL_UP_RIGHT``). A guild wearing one still gets a banner from
  its other layers, which is much closer to right than no banner at all.

Rasterisation is `resvg` rather than `cairosvg`: it ships self-contained
wheels for both Linux and Windows, so the image needs no `libcairo` and
the tests run on a developer's machine. It handles the `<style>` opacity
classes and the two gradient patterns natively, which a hand-rolled
rasteriser would have to grow.
"""

import asyncio
import hashlib
import io
import logging

import resvg_py
from PIL import Image

from hall_monitor.external import wynnpool

logger = logging.getLogger(__name__)

# Minecraft's sixteen dyes, as Wynnpool renders them. `SILVER` is the
# name Wynncraft's API actually returns; `LIGHT_GRAY` is the modern
# Minecraft spelling for the same dye and both appear in the wild.
DYES = {
    "WHITE": "#FFFFFF",
    "ORANGE": "#D87F33",
    "MAGENTA": "#B24CD8",
    "LIGHT_BLUE": "#6699D8",
    "YELLOW": "#E5E533",
    "LIME": "#7FCC19",
    "PINK": "#F27FA5",
    "GRAY": "#4C4C4C",
    "SILVER": "#999999",
    "LIGHT_GRAY": "#999999",
    "CYAN": "#4C7F99",
    "PURPLE": "#7F3FB2",
    "BLUE": "#334CB2",
    "BROWN": "#664C33",
    "GREEN": "#667F33",
    "RED": "#993333",
    "BLACK": "#191919",
}

# An unknown dye name. Visible enough to notice and report, rather than a
# transparent hole that reads as a rendering bug.
UNKNOWN_DYE = "#FF00FF"

# The pattern art's own viewBox, and so the resolution we composite at.
BANNER_SIZE = (160, 320)

# Discord scales emotes to a square. A banner is 1:2, so it's centred on
# a transparent square rather than stretched — half the canvas is wasted,
# but a stretched banner is a *different* banner, and recognising it is
# the entire point.
EMOTE_SIZE = 128


def dye(name: str) -> tuple[int, int, int]:
    """RGB for a Minecraft dye name, case-insensitively."""
    hex_colour = DYES.get((name or "").strip().upper())
    if hex_colour is None:
        logger.warning("banner: unknown dye %r; using the placeholder", name)
        hex_colour = UNKNOWN_DYE
    return tuple(int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))


async def render_banner(banner: wynnpool.Banner) -> bytes:
    """Compose ``banner`` and return an emote-sized PNG.

    Fetching the pattern art is async (and cached); the compositing is
    CPU work, so it goes to a thread — five layers of rasterisation on
    the event loop would stall the bot's gateway heartbeat, and a
    reconcile does this for every guild in the roster.
    """
    art = [
        (layer.colour, await wynnpool.banner_pattern(layer.pattern), layer.pattern)
        for layer in banner.layers
    ]
    return await asyncio.to_thread(_compose, banner.base, art)


def image_hash(png: bytes) -> str:
    """Stable identity for rendered bytes, so a re-upload only happens on
    a real change. An unconditional emote or role-icon write per hour is
    an audit-log entry per hour."""
    return hashlib.sha256(png).hexdigest()


# --------------------------------------------------------------------------
# Internals — all synchronous, all called on a worker thread
# --------------------------------------------------------------------------


def _compose(base: str, art: list[tuple[str, str | None, str]]) -> bytes:
    image = Image.new("RGBA", BANNER_SIZE, dye(base) + (255,))
    for colour, svg, pattern in art:
        if svg is None:
            logger.info("banner: no art published for %s; layer skipped", pattern)
            continue
        try:
            mask = _rasterise(svg)
        except Exception:  # noqa: BLE001 — one bad layer beats no banner
            logger.exception("banner: couldn't rasterise %s; layer skipped", pattern)
            continue
        image = Image.alpha_composite(image, _tint(mask, colour))
    return _to_emote(image)


def _rasterise(svg: str) -> Image.Image:
    """The pattern art as an image whose alpha channel is its coverage."""
    raw = bytes(resvg_py.svg_to_bytes(svg_string=svg))
    mask = Image.open(io.BytesIO(raw)).convert("RGBA")
    if mask.size != BANNER_SIZE:
        mask = mask.resize(BANNER_SIZE, Image.Resampling.LANCZOS)
    return mask


def _tint(mask: Image.Image, colour: str) -> Image.Image:
    """A solid sheet of ``colour`` cut to the mask's shape.

    The mask's *alpha* is the shape — the art's own colour is discarded.
    Partial alpha is Minecraft's shading and survives as partial coverage
    over whatever is underneath, which is what makes a stack of layers
    read as one banner rather than as flat decals.
    """
    layer = Image.new("RGBA", mask.size, dye(colour) + (255,))
    layer.putalpha(mask.getchannel("A"))
    return layer


def _to_emote(banner: Image.Image) -> bytes:
    """Centre the 1:2 banner on a transparent square and encode as PNG."""
    height = EMOTE_SIZE
    width = round(height * BANNER_SIZE[0] / BANNER_SIZE[1])
    canvas = Image.new("RGBA", (EMOTE_SIZE, EMOTE_SIZE), (0, 0, 0, 0))
    canvas.paste(
        banner.resize((width, height), Image.Resampling.LANCZOS),
        ((EMOTE_SIZE - width) // 2, 0),
    )
    buffer = io.BytesIO()
    canvas.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()
