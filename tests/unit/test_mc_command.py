"""Parse `hall request 14`; reject unknown subcommand and non-integer arg."""

import pytest

from hall_monitor.services.mc_command import (
    InvalidArguments,
    RequestCommand,
    UnknownSubcommand,
    parse,
)


def test_parse_request_command():
    assert parse("request 14") == RequestCommand(bits=14)


def test_parse_request_command_is_case_insensitive():
    assert parse("REQUEST 3") == RequestCommand(bits=3)


def test_parse_tolerates_leading_and_trailing_whitespace():
    assert parse("  request 7  ") == RequestCommand(bits=7)


def test_reject_unknown_subcommand():
    with pytest.raises(UnknownSubcommand):
        parse("dance 1")


def test_reject_empty():
    with pytest.raises(UnknownSubcommand):
        parse("")


def test_reject_non_integer_argument():
    with pytest.raises(InvalidArguments):
        parse("request three")


def test_reject_missing_argument():
    with pytest.raises(InvalidArguments):
        parse("request")


def test_reject_extra_arguments():
    with pytest.raises(InvalidArguments):
        parse("request 1 2")
