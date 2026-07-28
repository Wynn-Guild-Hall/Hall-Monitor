"""Encoding for the role-bits integer inside a ``HALL<NN>`` code.

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


# An observer holds no contact roles at all, so "which roles" is the
# wrong question rather than a question with an empty answer — an empty
# set would decode indistinguishably from `HALL00`, a delegate who asked
# for nothing. `-1` is unreachable from the MC side by construction: a
# `HALL<NN>` code parses to 0–99, so a negative value can only ever have
# been written here, by `~force observer`.
OBSERVER = -1


class UnknownRoleBit(ValueError):
    """Raised when an integer sets a bit that isn't in ``ROLE_BITS``."""


def is_observer(value: int) -> bool:
    """Whether this ``roles_bits`` marks an observer invite rather than roles.

    Callers must ask *before* :func:`decode`, which raises on the
    sentinel — deliberately, so anything that treats an observer invite
    as a role set fails loudly instead of quietly applying nothing.
    """
    return value == OBSERVER


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
