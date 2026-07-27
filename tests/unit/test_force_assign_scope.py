"""Scoping for ``~force assign``: janitors reach any guild, ownership
contacts only their own, and the target has to be a delegate either way."""

import pytest

from hall_monitor.discord_bot.cogs.force.assign import (
    Rejected,
    parse_contact_type,
    resolve_guild_tag,
)


def test_janitor_assigns_within_the_targets_guild():
    assert (
        resolve_guild_tag("VETS", "OTHR", caller_is_janitor=True) == "OTHR"
    )


def test_ownership_contact_confined_to_their_own_guild():
    with pytest.raises(Rejected):
        resolve_guild_tag("VETS", "OTHR", caller_is_janitor=False)


def test_ownership_contact_assigns_within_their_guild():
    assert resolve_guild_tag("VETS", "VETS", caller_is_janitor=False) == "VETS"


def test_same_guild_check_folds_case():
    """The caller's row and the target's row can disagree on spelling — the
    API's capitalisation is whatever it was when each of them verified."""
    assert resolve_guild_tag("VETS", "vets", caller_is_janitor=False) == "VETS"


def test_unregistered_target_rejected_for_janitor():
    with pytest.raises(Rejected):
        resolve_guild_tag("VETS", None, caller_is_janitor=True)


def test_unregistered_target_rejected_for_ownership_contact():
    with pytest.raises(Rejected):
        resolve_guild_tag("VETS", None, caller_is_janitor=False)


def test_unregistered_caller_rejected_when_not_a_janitor():
    """An ownership contact the bot has no delegate row for has no guild to
    scope to — better a clear refusal than a guess."""
    with pytest.raises(Rejected):
        resolve_guild_tag(None, "VETS", caller_is_janitor=False)


def test_unregistered_caller_fine_for_a_janitor():
    assert resolve_guild_tag(None, "VETS", caller_is_janitor=True) == "VETS"


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("events", "events"),
        ("Housing", "housing"),
        ("  warring  ", "warring"),
        ("ownership contact", "ownership"),
        ("Events_Contact", "events"),
    ],
)
def test_contact_type_normalisation(typed, expected):
    assert parse_contact_type(typed) == expected


def test_unknown_contact_type_lists_the_valid_ones():
    with pytest.raises(Rejected) as exc:
        parse_contact_type("recruitment")
    for name in ("events", "housing", "warring", "ownership"):
        assert name in str(exc.value)
