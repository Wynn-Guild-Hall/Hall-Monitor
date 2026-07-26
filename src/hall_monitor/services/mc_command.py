"""Parser for the MC chat sub-command in ``hall <subcommand> <args>`` payloads.

Picolimbo strips the ``hall `` prefix before forwarding; this module accepts
the remainder and dispatches to a subcommand handler.
"""

from dataclasses import dataclass


class UnknownSubcommand(ValueError):
    pass


class InvalidArguments(ValueError):
    pass


@dataclass(frozen=True)
class RequestCommand:
    bits: int


def parse(msg: str) -> RequestCommand:
    parts = msg.strip().split()
    if not parts:
        raise UnknownSubcommand("")
    subcommand, *args = parts
    if subcommand.lower() == "request":
        if len(args) != 1:
            raise InvalidArguments(f"expected 1 argument, got {len(args)}")
        try:
            return RequestCommand(bits=int(args[0]))
        except ValueError as exc:
            raise InvalidArguments(str(exc)) from exc
    raise UnknownSubcommand(subcommand)
