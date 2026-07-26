"""Role-gate decorators for prefix commands.

Every gate reads its role IDs from ``Settings`` so no ID is hard-coded.
"""

from discord.ext import commands

from hall_monitor.config import settings

CONTACT_ROLE_IDS = (
    settings.ownership_contact_role_id,
    settings.warring_contact_role_id,
    settings.housing_contact_role_id,
    settings.events_contact_role_id,
)


def _has_any_role(*role_ids: int):
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None or not hasattr(ctx.author, "roles"):
            return False
        author_roles = {r.id for r in ctx.author.roles}
        return any(rid in author_roles for rid in role_ids if rid)

    return commands.check(predicate)


def is_delegate():
    return _has_any_role(settings.delegate_role_id)


def is_contact():
    return _has_any_role(*CONTACT_ROLE_IDS)


def is_ownership_contact():
    return _has_any_role(settings.ownership_contact_role_id)


def is_janitor():
    return _has_any_role(settings.janitor_role_id, settings.monitor_role_id)


def is_monitor():
    return _has_any_role(settings.monitor_role_id)
