"""Tag comparison: case-agnostic in practice, case-preserving in storage."""

from hall_monitor.services.guild_tag import matches, normalise


def test_case_is_ignored_when_comparing():
    assert matches("VETS", "vets")
    assert matches("Vets", "vETS")


def test_surrounding_whitespace_is_ignored():
    assert matches(" VETS ", "VETS")
    assert normalise("  vets\n") == "vets"


def test_interior_spaces_and_underscores_are_preserved():
    """Both are legal in a tag, rare as that is — normalise must not
    strip or fold them into each other."""
    assert normalise("My Guild") == "my guild"
    assert normalise("My_Guild") == "my_guild"
    assert not matches("My Guild", "My_Guild")
    assert matches("My Guild", "my guild")


def test_different_tags_do_not_match():
    assert not matches("VETS", "VET")
    assert not matches("VETS", "OTHR")


def test_none_matches_nothing():
    assert not matches(None, "VETS")
    assert not matches("VETS", None)
    assert not matches(None, None)


def test_normalise_does_not_change_length_of_a_plain_tag():
    """Storage is capped at 8 characters; folding must not inflate one."""
    assert len(normalise("VETS")) == len("VETS")
