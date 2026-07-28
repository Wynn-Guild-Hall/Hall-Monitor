"""The ``Username [TAG]`` nickname shape, and the rule for enforcing it.

Every representative wears their guild's tag in the member list, so a
conversation with someone in the Hall carries who they speak for without
anyone having to check a roster. The tag is chosen the same way the
standing role is (``delegate_registry.standing``): their guild's, or
``[EXT]`` once they've moved elsewhere.

**Only the suffix is ours.** The visible part is whatever the member set
— a nickname, or their Minecraft username on the way in — and enforcement
re-attaches the tag rather than replacing what they chose. Somebody
renaming themselves is not doing anything wrong; they just can't drop the
tag while doing it.

Nobody without a ``Delegate`` row is touched. Observers are excluded
outright, and so is anyone we simply don't have on file — staff, bots,
guests. Renaming a member the Hall knows nothing about would be a bot
overreaching well past what it was invited to do.
"""

import logging
import re

import discord

from hall_monitor.config import settings
from hall_monitor.db.models import Delegate
from hall_monitor.services import delegate_registry

logger = logging.getLogger(__name__)

# Discord's hard cap on a nickname.
NICK_LIMIT = 32

# The tag worn by a representative who has moved to a different guild.
EXTERNAL_TAG = "EXT"

# One trailing bracketed token — the shape this module writes. Only one is
# stripped: a member whose chosen name genuinely ends in `[something]`
# keeps it, and gets the guild tag appended after it.
_TRAILING_TAG = re.compile(r"\s*\[[^\[\]]*\]\s*$")


def visible_part(nick: str) -> str:
    """``nick`` with a trailing ``[TAG]`` removed, if it has one."""
    return _TRAILING_TAG.sub("", nick).strip()


def desired(visible: str, tag: str) -> str:
    """The nickname ``visible`` should become for ``tag``.

    Truncates the visible part rather than the tag when the pair won't fit
    in Discord's 32 characters: the tag is the part that carries meaning
    to everyone else in the room.
    """
    suffix = f" [{tag}]"
    trimmed = visible.strip()[: NICK_LIMIT - len(suffix)].strip()
    return f"{trimmed}{suffix}" if trimmed else suffix.strip()


async def tag_for(delegate: Delegate) -> str:
    return (
        EXTERNAL_TAG
        if await delegate_registry.is_external(delegate)
        else delegate.guild_tag
    )


def is_observer(member: discord.Member) -> bool:
    observer_role_id = settings.observer_role_id
    return bool(observer_role_id) and any(
        role.id == observer_role_id for role in member.roles
    )


async def enforce(member: discord.Member, *, reason: str | None = None) -> bool:
    """Put the right tag on ``member``'s nickname. Returns whether it moved.

    A no-op for anyone the Hall doesn't name, and for a nickname that's
    already right — this runs on every member update, and `on_member_update`
    fires once per role added, so the quiet path has to stay free.
    """
    if member.bot or is_observer(member):
        return False
    delegate = await delegate_registry.get_by_discord_user_id(member.id)
    if delegate is None:
        return False

    # No nickname yet means they just arrived: their Minecraft username is
    # the name the Hall knows them by, and it's what the roster will show.
    base = member.nick or delegate.mc_username or member.name
    wanted = desired(visible_part(base), await tag_for(delegate))
    if member.nick == wanted:
        return False

    try:
        await member.edit(nick=wanted, reason=reason or "hall-monitor: guild tag")
    except discord.Forbidden:
        # Server owners can't be renamed by anyone, and neither can anyone
        # above the bot in the role list. Both are configuration, not bugs.
        logger.warning(
            "nickname: not allowed to rename %s (owner, or above me in the "
            "role list) — wanted %r",
            member.id,
            wanted,
        )
        return False
    except discord.HTTPException:
        logger.exception("nickname: couldn't rename %s to %r", member.id, wanted)
        return False
    logger.info("nickname: %s is now %r", member.id, wanted)
    return True
