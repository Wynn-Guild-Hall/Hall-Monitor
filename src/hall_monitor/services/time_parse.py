"""Parse short duration strings used by ``~force <sub> <target> <time>``.

Accepted forms
--------------
- ``"7d"``  → 7 days
- ``"2w"``  → 14 days
- ``"3mo"`` → 90 days (1 month treated as 30 days)
- ``"1y"``  → 365 days
- ``"0"``   → :data:`PERMANENT` sentinel (returned as :data:`None`); reserved
  for monitor-tier callers who intend "no expiry".

The units the plan calls out are ``d/w/mo/y`` — nothing shorter (no
minutes/hours). An override measured in minutes would expire before
anyone noticed it, and every gate we ship is bounded in months.
"""

import re
from datetime import timedelta


class InvalidDuration(ValueError):
    """Raised for anything that isn't ``0`` or ``<int><d|w|mo|y>``."""


PERMANENT: None = None

_UNIT_DAYS = {
    "d": 1,
    "w": 7,
    "mo": 30,
    "y": 365,
}
# Longest unit first so ``mo`` wins over ``m`` (m is unsupported, but this
# also future-proofs against adding minute/month collisions).
_PATTERN = re.compile(
    r"^\s*(\d+)\s*(" + "|".join(sorted(_UNIT_DAYS, key=len, reverse=True)) + r")\s*$"
)


def parse(text: str) -> timedelta | None:
    """Returns a :class:`timedelta`, or :data:`None` for a permanent override.

    Raises :class:`InvalidDuration` on anything else.
    """
    stripped = text.strip()
    if stripped == "0":
        return PERMANENT
    match = _PATTERN.match(stripped.lower())
    if not match:
        raise InvalidDuration(text)
    quantity = int(match.group(1))
    if quantity <= 0:
        raise InvalidDuration(text)
    return timedelta(days=quantity * _UNIT_DAYS[match.group(2)])
