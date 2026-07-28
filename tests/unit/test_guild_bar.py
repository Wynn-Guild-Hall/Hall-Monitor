"""Rows of squares, and the attributed form with guild banners over them.

The attributed form is deliberately *not* used by the expel vote (see
`test_expel.py`); these cover it as the reusable thing it is.
"""

from unittest.mock import MagicMock

from hall_monitor.db.models import GuildEmote
from hall_monitor.services import guild_bar, roster


class FakeEmoji:
    def __init__(self, emoji_id: int, name: str):
        self.id = emoji_id
        self.name = name

    def __str__(self) -> str:
        return f"<:{self.name}:{self.id}>"


def _guild(*emojis):
    guild = MagicMock()
    guild.emojis = list(emojis)
    return guild


def test_a_plain_row_is_just_the_squares():
    assert guild_bar.row([guild_bar.GREEN, guild_bar.RED]) == (
        guild_bar.GREEN + guild_bar.RED
    )


def test_the_palette_is_unicode_not_shortcodes():
    """Discord expands `:green_square:` in its client when a human presses
    send. A bot posting the shortcode posts the shortcode."""
    for square in guild_bar.SQUARES.values():
        assert not square.startswith(":")
        assert len(square) == 1


async def test_an_attributed_bar_puts_each_banner_over_its_own_square(db):
    await GuildEmote.create(
        guild_tag="VETS", discord_emoji_id=11, image_hash="a"
    )
    await GuildEmote.create(guild_tag="ANO", discord_emoji_id=12, image_hash="b")
    guild = _guild(FakeEmoji(11, "VETS"), FakeEmoji(12, "ANO"))

    bar = await guild_bar.attributed(
        guild, [("VETS", guild_bar.GREEN), ("ANO", guild_bar.RED)]
    )

    emotes, squares = bar.split("\n")
    assert emotes == "<:VETS:11><:ANO:12>"
    assert squares == guild_bar.GREEN + guild_bar.RED


async def test_the_two_rows_always_hold_the_same_number_of_glyphs(db):
    """They wrap identically only while they do — and a sheared bar is
    worse than no bar, because it silently reattributes every square."""
    guild = _guild()
    cells = [(f"G{index}", guild_bar.GREEN) for index in range(9)]

    lines = (await guild_bar.attributed(guild, cells, per_row=4)).split("\n")

    assert len(lines) == 6, "three pairs of lines"
    for emotes, squares in zip(lines[::2], lines[1::2]):
        assert len(emotes.split(roster.FALLBACK_EMOTE)) - 1 == len(squares)


async def test_a_guild_with_no_minted_banner_still_renders(db):
    """A missing emote must never be the thing that stops a bar, for the
    same reason it must never stop the roster."""
    guild = _guild()

    bar = await guild_bar.attributed(guild, [("NEVR", guild_bar.WHITE)])

    assert bar == f"{roster.FALLBACK_EMOTE}\n{guild_bar.WHITE}"


async def test_an_empty_bar_is_empty_rather_than_two_blank_lines(db):
    assert await guild_bar.attributed(_guild(), []) == ""
