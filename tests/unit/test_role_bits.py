"""Encoding round-trip + rejection of integers containing bits outside ROLE_BITS."""

import pytest

from hall_monitor.services.role_bits import (
    ROLE_BITS,
    UnknownRoleBit,
    decode,
    encode,
)


def test_encode_decode_round_trip_all_subsets():
    """Every subset of the current ROLE_BITS survives encode→decode."""
    names = list(ROLE_BITS.values())
    for mask in range(1 << len(names)):
        subset = {names[i] for i in range(len(names)) if mask & (1 << i)}
        assert decode(encode(subset)) == subset


def test_decode_events_only():
    assert decode(0b0001) == {"events"}


def test_decode_events_and_warring():
    assert decode(0b0101) == {"events", "warring"}


def test_decode_all_four():
    assert decode(0b1111) == {"events", "housing", "warring", "ownership"}


def test_decode_zero_is_empty_set():
    assert decode(0) == set()


def test_reject_bit_above_defined_range():
    """Bit 4 isn't in ROLE_BITS yet — the spec says it's reserved."""
    with pytest.raises(UnknownRoleBit):
        decode(1 << 4)


def test_reject_bit_mixed_with_valid():
    """A code that sets both a real bit and an unknown one is still rejected wholesale."""
    with pytest.raises(UnknownRoleBit):
        decode(0b10001)


def test_reject_negative():
    with pytest.raises(UnknownRoleBit):
        decode(-1)
