"""Parser for the Guild Hall code a representative types in Minecraft chat.

The code is ``HALL<NN>`` — a fixed marker followed by the role-bits
integer, zero-padded to fill the same six characters a dazebot
account-link code occupies. The verify server asks for "your code" and
gets one shape back either way, which is the whole point of the padding.

The two code spaces can't collide. dazebot draws from an alphabet with no
``L`` in it (visually-confusable characters are excluded), so no link code
it issues can begin with ``HALL``.

Picolimbo matches the ``hall`` route prefix and strips it, so what
normally arrives here is the bare number. The marker is accepted too,
which keeps a hand-run curl identical to what the player typed.
"""

import re
from dataclasses import dataclass

CODE_MARKER = "HALL"

# Four digits is far more headroom than ROLE_BITS will plausibly need and
# still refuses a line that's mostly digits by accident.
_CODE_RE = re.compile(rf"^(?:{CODE_MARKER})?(\d{{1,4}})$", re.IGNORECASE)


class InvalidCode(ValueError):
    """The chat line isn't a Guild Hall code."""


@dataclass(frozen=True)
class RequestCommand:
    bits: int


def parse(msg: str) -> RequestCommand:
    match = _CODE_RE.match(msg.strip())
    if match is None:
        raise InvalidCode(msg)
    return RequestCommand(bits=int(match.group(1)))


def format_code(bits: int) -> str:
    """The code to hand a representative for ``bits``.

    Hallway builds the same string in JS for its live display — change one
    and change the other.
    """
    return f"{CODE_MARKER}{bits:02d}"


def looks_like_attempt(msg: str) -> bool:
    """Whether a line routed to us was plausibly meant as a code.

    The route prefix is only ``hall``, so ordinary chat starting with those
    letters lands here too. A line carrying a digit gets told it's not a
    valid code; anything else is dropped in silence rather than
    disconnecting someone for saying "hallo".
    """
    return any(character.isdigit() for character in msg)
