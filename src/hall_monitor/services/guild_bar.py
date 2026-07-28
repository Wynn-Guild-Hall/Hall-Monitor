"""Rows of coloured squares, optionally captioned with guild banners.

Two shapes, and choosing between them is a decision about people rather
than about layout:

- :func:`row` — squares alone. A count made visible, attributable to
  nobody.
- :func:`attributed` — each guild's banner emote with its own square
  directly beneath, so the row reads guild by guild:

  ```
  :SEQ::Aeq::TAq::AVO::PUN::ANO:
  🟩🟩🟩⬜⬜🟥
  ```

**The expel vote deliberately uses the first.** A bar with names on it is
a durable, screenshottable record of who voted to remove whom, and the
whole point of that vote is that a guild can leave without anyone being
left with somebody to blame (DESIGN.md §16.3). This module exists so the
attributed form is available to everything *else* — turnouts, sign-ups,
territory splits, anything where being able to see which guild is which
is the point rather than the hazard.

Two things about the rendering that aren't obvious:

- **The two rows have to wrap identically or the columns stop lining
  up.** Discord renders a custom emote and a unicode square at the same
  inline size, so they do — as long as both rows hold the same number of
  glyphs, which is what ``per_row`` is for on a bar long enough to wrap
  on a narrow client.
- **Unicode literals, not `:green_square:`.** Discord expands shortcodes
  in its *client* when a human presses send; a bot posting the shortcode
  posts the shortcode.
"""

from collections.abc import Iterable, Sequence

import discord

from hall_monitor.services import roster

GREEN = "\N{LARGE GREEN SQUARE}"
WHITE = "\N{WHITE LARGE SQUARE}"
RED = "\N{LARGE RED SQUARE}"
BLUE = "\N{LARGE BLUE SQUARE}"
YELLOW = "\N{LARGE YELLOW SQUARE}"
ORANGE = "\N{LARGE ORANGE SQUARE}"
PURPLE = "\N{LARGE PURPLE SQUARE}"
BROWN = "\N{LARGE BROWN SQUARE}"
BLACK = "\N{BLACK LARGE SQUARE}"

SQUARES = {
    "green": GREEN,
    "white": WHITE,
    "red": RED,
    "blue": BLUE,
    "yellow": YELLOW,
    "orange": ORANGE,
    "purple": PURPLE,
    "brown": BROWN,
    "black": BLACK,
}


def row(squares: Iterable[str]) -> str:
    """Squares alone, in the order given, with nothing identifying them."""
    return "".join(squares)


async def attributed(
    discord_guild: discord.Guild,
    cells: Sequence[tuple[str, str]],
    *,
    per_row: int | None = None,
) -> str:
    """Guild banners over their squares. ``cells`` is ``(guild_tag, square)``.

    Order is whatever the caller passes — this doesn't sort, because the
    ordering is usually the point (roster order, level rank, turnout) and
    is the caller's to decide.

    ``per_row`` breaks the bar into aligned pairs of lines. Leave it
    ``None`` for one long row, which is what looks right up to a few
    dozen guilds; set it when the list is long enough that a narrow
    client would wrap the two rows at different points and shear the
    columns apart.

    Emotes resolve through ``roster.emote_for``, so a guild without a
    minted banner wears the shared blank one and a server missing even
    that still renders — a missing emote must never be the thing that
    stops a bar, for the same reason it must never stop the roster.
    """
    if not cells:
        return ""
    minted = await roster.minted_emotes()
    size = per_row or len(cells)
    lines = []
    for start in range(0, len(cells), size):
        chunk = cells[start : start + size]
        lines.append(
            "".join(roster.emote_for(discord_guild, tag, minted) for tag, _ in chunk)
        )
        lines.append(row(square for _, square in chunk))
    return "\n".join(lines)
