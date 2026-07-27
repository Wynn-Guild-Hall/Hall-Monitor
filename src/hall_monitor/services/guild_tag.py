"""Canonical handling for Wynncraft guild tags (prefixes).

A tag is normally three or four characters and case-agnostic: ``VETS``
and ``vets`` are the same guild. Two rarities the API permits, which this
module exists to keep from surprising us:

1. **Case can, in principle, be significant.** So the spelling Wynncraft
   gives is what we store and display — only *comparison* folds case.
   Uppercasing on the way in would throw away information we were handed.
2. **Spaces and underscores are legal characters.** Nothing here may
   assume a tag is a single word, or that it's safe to split on.

The case-folding matters most where a human types the tag. A janitor
running ``~force notable vets 3mo`` will not reliably match the API's
capitalisation, and an override that silently applies to a different key
than the one notability looks up is an override that does nothing.

Discord's own argument parser handles rarity 2 already: a tag containing
a space needs quoting at the call site — ``~force notable "My Guild" 3mo``
— which discord.py splits correctly.
"""


def normalise(tag: str) -> str:
    """The comparison key for a tag.

    Trims surrounding whitespace and folds case. Interior characters are
    left exactly as they are, spaces and underscores included.
    """
    return tag.strip().casefold()


def matches(left: str | None, right: str | None) -> bool:
    """Whether two tags refer to the same guild. ``None`` matches nothing."""
    if left is None or right is None:
        return False
    return normalise(left) == normalise(right)
