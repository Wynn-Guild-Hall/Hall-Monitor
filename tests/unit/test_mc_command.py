"""Parse the `HALL<NN>` code, with and without the marker picolimbo strips."""

import pytest

from hall_monitor.services.mc_command import (
    CODE_MARKER,
    InvalidCode,
    RequestCommand,
    format_code,
    looks_like_attempt,
    parse,
)
from hall_monitor.services.role_bits import ROLE_BITS

# dazebot's link-code alphabet, from lib/mc/linking.py. Visually-confusable
# characters are excluded, and that exclusion is what keeps the two code
# spaces from overlapping.
DAZEBOT_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def test_parse_bare_digits_as_picolimbo_forwards_them():
    """The route prefix is stripped upstream, so this is the normal case."""
    assert parse("14") == RequestCommand(bits=14)


def test_parse_accepts_the_marker_the_player_typed():
    assert parse("HALL14") == RequestCommand(bits=14)


def test_parse_is_case_insensitive():
    assert parse("hall03") == RequestCommand(bits=3)


def test_parse_reads_through_the_zero_padding():
    assert parse("HALL05") == RequestCommand(bits=5)
    assert parse("HALL00") == RequestCommand(bits=0)


def test_parse_tolerates_surrounding_whitespace():
    assert parse("  HALL07  ") == RequestCommand(bits=7)


def test_reject_ordinary_chat():
    with pytest.raises(InvalidCode):
        parse("o everyone")


def test_reject_empty():
    with pytest.raises(InvalidCode):
        parse("")


def test_reject_a_code_with_a_typo_in_the_digits():
    with pytest.raises(InvalidCode):
        parse("HALL1X")


def test_reject_a_marker_with_no_digits():
    with pytest.raises(InvalidCode):
        parse("HALL")


def test_reject_a_negative():
    with pytest.raises(InvalidCode):
        parse("-1")


def test_format_pads_to_the_six_characters_a_link_code_occupies():
    assert format_code(0) == "HALL00"
    assert format_code(5) == "HALL05"
    assert format_code(15) == "HALL15"
    for bits in range(1 << len(ROLE_BITS)):
        assert len(format_code(bits)) == 6


def test_every_formatted_code_parses_back():
    for bits in range(1 << len(ROLE_BITS)):
        assert parse(format_code(bits)).bits == bits


def test_marker_cannot_collide_with_a_dazebot_link_code():
    """dazebot's alphabet has no L, so nothing it issues starts with HALL.
    Repointing the marker at characters it *can* produce would put a
    representative's code and someone's link code in the same space."""
    assert any(character not in DAZEBOT_ALPHABET for character in CODE_MARKER)


def test_a_line_with_a_digit_reads_as_an_attempted_code():
    assert looks_like_attempt("HALL1X")
    assert looks_like_attempt("15 16")


def test_plain_chat_does_not_read_as_an_attempted_code():
    """`hall` is the whole route prefix, so conversation lands here too."""
    assert not looks_like_attempt("o everyone")
    assert not looks_like_attempt("")
