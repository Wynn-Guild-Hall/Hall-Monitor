"""Encoding for the ``hall request <N>`` role-bits integer.

Bit indices are stable — new roles get appended to ``ROLE_BITS`` at a fresh
index. Old codes stay valid; codes carrying an unknown bit are rejected.
"""

RoleName = str

ROLE_BITS: dict[int, RoleName] = {
    0: "events",
    1: "housing",
    2: "warring",
    3: "ownership",
}


class UnknownRoleBit(ValueError):
    """Raised when an integer sets a bit that isn't in ``ROLE_BITS``."""


def decode(value: int) -> set[RoleName]:
    if value < 0:
        raise UnknownRoleBit(value)
    roles: set[RoleName] = set()
    remaining = value
    bit = 0
    while remaining:
        if remaining & 1:
            if bit not in ROLE_BITS:
                raise UnknownRoleBit(bit)
            roles.add(ROLE_BITS[bit])
        remaining >>= 1
        bit += 1
    return roles


def encode(roles: set[RoleName]) -> int:
    reverse = {name: bit for bit, name in ROLE_BITS.items()}
    value = 0
    for role in roles:
        value |= 1 << reverse[role]
    return value
