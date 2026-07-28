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

# Janitors get a three-month ceiling on every `~force`: long enough to
# carry a guild through a quiet patch, short enough that nobody can
# quietly park one state or another forever. There's no floor — a janitor
# who wants to grant a week has a reason, and a short override expires on
# its own anyway.
JANITOR_MAX = timedelta(days=90)

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


def gating_rejection(delta: timedelta | None, is_monitor: bool) -> str | None:
    """Whether this caller may use this duration, as a user-facing message.

    Returns ``None`` when the request should proceed. Pure, so the rule is
    unit-testable without faking a Discord context, and shared: every
    `~force` sub-command applies the same ceiling, and a second copy of it
    is a second thing to forget to change.
    """
    if is_monitor:
        return None
    if delta is None:
        return "permanent overrides are monitor-only; try `3mo` or shorter."
    if delta > JANITOR_MAX:
        return (
            "janitor overrides can't run past three months (`3mo`); "
            "ask a monitor for anything longer."
        )
    return None
