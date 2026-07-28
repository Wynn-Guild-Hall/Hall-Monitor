"""The keys a guild can answer, declared here and nowhere else.

**Keys are declared, not invented.** A contact sets the *value* of a key;
they cannot bring a new key into existence. That's not tidiness — the
consumer is a template, not a dump. A Hallway page renders these into a
layout with headings and labels, so a guild that invented
``recruitment_status_2`` would produce a value nothing knows how to show,
and one that wrote ``recruitmentStatus`` would silently drop off a
comparison the page was trying to draw.

It also makes **unset** a real answer. With a fixed set, a guild that
hasn't filled something in renders as "unset", which is information;
under free-form keys an absent key was indistinguishable from one nobody
had thought of.

Adding a key is one entry below — no migration, no other change, and it
becomes settable immediately. Removing one leaves any stored rows
orphaned; they're ignored on read, so a key can be retired and restored
without losing what guilds had written.

**No lists.** A multi-value answer is a scalar with a convention —
comma-separated, or one per line — chosen by whoever first has a key
that wants one, because that's a decision with a real consumer attached.
:data:`Kind` is where ``list`` would slot in if it's ever wanted, and
nothing here changes shape when it does.
"""

from dataclasses import dataclass

# What a key holds, and therefore which command writes it.
BOOL = "bool"
SCALAR = "scalar"

DEFAULT_MAX_LENGTH = 512

TRUTHY = {"true", "yes", "y", "on", "1", "open"}
FALSEY = {"false", "no", "n", "off", "0", "closed"}


class UnknownKey(KeyError):
    """No such key is declared."""


class WrongKind(ValueError):
    """The key exists but takes a different command."""

    def __init__(self, key: "Key", tried: str) -> None:
        super().__init__(key.name)
        self.key = key
        self.tried = tried


class BadValue(ValueError):
    """The value doesn't fit the key."""


@dataclass(frozen=True)
class Key:
    name: str
    kind: str
    description: str
    max_length: int = DEFAULT_MAX_LENGTH

    @property
    def command(self) -> str:
        """The subcommand that writes this kind."""
        return "toggle" if self.kind == BOOL else "set"


# Deliberately empty, and it stays that way until something *reads*
# these. Nothing renders a guild's answers yet — the Hallway page is out
# of scope — and a key that can be filled in but is displayed nowhere
# invites guilds to write things into a place nobody is looking. Worse
# than a missing feature: it looks like a working one.
#
# A key arrives when its consumer does. Add the entry here, no migration,
# settable immediately:
#
#     Key("recruiting", BOOL, "Whether you're taking applications"),
#
# The commands read correctly with none declared — `~dash` says the Hall
# isn't asking anything yet rather than printing an empty list.
KEYS: dict[str, Key] = {}


def get(name: str) -> Key:
    """The declared key, matched case-insensitively.

    Folded because a human types these. ``Recruiting`` and ``recruiting``
    are the same question, and refusing the first would be a puzzle
    rather than a rule.
    """
    try:
        return KEYS[name.strip().casefold()]
    except KeyError as exc:
        raise UnknownKey(name) from exc


def require(name: str, kind: str) -> Key:
    """The declared key, insisting it takes ``kind``.

    Raises :class:`WrongKind` rather than :class:`UnknownKey` when the key
    is real but the wrong shape, so the caller can say *which* command it
    does take instead of "invalid".
    """
    key = get(name)
    if key.kind != kind:
        raise WrongKind(key, kind)
    return key


def parse_bool(raw: str) -> bool:
    """Read a human's yes/no. Raises :class:`BadValue` on anything else."""
    folded = raw.strip().casefold()
    if folded in TRUTHY:
        return True
    if folded in FALSEY:
        return False
    raise BadValue(raw)


def clean_scalar(key: Key, raw: str) -> str:
    """Trim and length-check a scalar. Raises :class:`BadValue` if too long.

    Refused rather than truncated: a value silently cut at 512 characters
    is worse than one that didn't save, because nothing tells the author
    the end of their sentence is gone.
    """
    value = raw.strip()
    if not value:
        raise BadValue("")
    if len(value) > key.max_length:
        raise BadValue(value)
    return value


def render(key: Key, value: object) -> str:
    """How one stored value reads in the listing."""
    if value is None:
        return "_unset_"
    if key.kind == BOOL:
        return "yes" if value else "no"
    return f"`{value}`"
