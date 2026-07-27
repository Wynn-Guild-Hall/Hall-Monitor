"""Role-gate decorators for prefix commands.

**The tiers nest.** A janitor can do anything a contact can, and a
monitor anything a janitor can, so every gate here also admits the staff
roles above it. A monitor being told they haven't got the role for a
command they administer is a bug, not a policy — and the janitor gate
already worked this way, which is what made the inconsistency easy to
miss. ``is_monitor`` is the exception, being the top tier.

Every gate reads its role IDs from ``Settings`` at call time rather than
at import: they're deploy-time config, and tests monkeypatch them.
"""

from discord.ext import commands

from hall_monitor.config import settings

CONTACT_ROLE_ATTRS = (
    "ownership_contact_role_id",
    "warring_contact_role_id",
    "housing_contact_role_id",
    "events_contact_role_id",
)

STAFF_ROLE_ATTRS = ("janitor_role_id", "monitor_role_id")


def has_any_role(ctx: commands.Context, *role_ids: int) -> bool:
    """Plain-boolean form of the gates below.

    A command whose *behaviour* differs by tier — not just its
    availability — has to branch inside the handler, where a check
    decorator can't help. Unset IDs (``0``) never match.
    """
    if ctx.guild is None or not hasattr(ctx.author, "roles"):
        return False
    author_roles = {r.id for r in ctx.author.roles}
    return any(rid in author_roles for rid in role_ids if rid)


def _gate(*attributes: str, staff: bool = True):
    """A check for the named settings roles, plus the staff tiers above them."""

    async def predicate(ctx: commands.Context) -> bool:
        wanted = (*attributes, *(STAFF_ROLE_ATTRS if staff else ()))
        return has_any_role(ctx, *(getattr(settings, name) for name in wanted))

    return commands.check(predicate)


def is_delegate():
    return _gate("delegate_role_id", *CONTACT_ROLE_ATTRS)


def is_contact():
    return _gate(*CONTACT_ROLE_ATTRS)


def is_ownership_contact():
    return _gate("ownership_contact_role_id")


def is_janitor():
    return _gate()  # staff-only, which the default already covers


def is_monitor():
    return _gate("monitor_role_id", staff=False)
