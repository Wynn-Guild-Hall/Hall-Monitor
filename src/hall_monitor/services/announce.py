"""Telling the Hall who just arrived.

A line in delegate general whenever the bot admits somebody, so the room
knows who the new name is and what they're there for. Without it a
representative appears in the member list with a tag nobody watched them
earn, and the only record is a log line on the VPS.

**It notifies nobody.** The new member is named with a real `<@id>` so
the line links through to them, but the message is sent with
``AllowedMentions.none()`` — this channel's one deliberate ping is the
expel call (DESIGN.md §16.4), gated on three guilds agreeing, and a join
announcement is not that. Pinging the new arrival on their way in would
be the worst version: their first experience of the Hall is a
notification about themselves.

**Only arrivals the bot actually processed are announced.** A join that
matched no pending invite is already logged and is nothing a delegate
can act on; "somebody joined and I don't know who" in delegate general
is noise, and the sort that trains people to stop reading a channel.

Best-effort throughout. A failed announcement is logged and the
verification stands — the roles are what matter and they're already on.
"""

import logging

import discord

from hall_monitor.config import settings
from hall_monitor.db.models import MajorGuildCache
from hall_monitor.services import guild_tag as tags

logger = logging.getLogger(__name__)

# Renders mentions without ringing anybody. See the module docstring.
SILENT = discord.AllowedMentions.none()


def channel(discord_guild: discord.Guild) -> discord.TextChannel | None:
    """Delegate general, or ``None`` if it isn't configured or reachable."""
    if not settings.delegate_channel_id:
        logger.debug("announce: no delegate channel configured")
        return None
    found = discord_guild.get_channel(settings.delegate_channel_id)
    if found is None:
        logger.warning(
            "announce: delegate channel %s is not in the guild",
            settings.delegate_channel_id,
        )
    return found


async def guild_label(guild_tag: str) -> str:
    """``**Corrosion** (`Crrs`)`` where the name is known, else the tag.

    The name comes from the cache the hourly sweep already fills, so this
    costs a query rather than a third-party request — and a guild we've
    never swept still reads correctly, just more tersely.
    """
    row = await MajorGuildCache.filter(guild_tag__iexact=guild_tag).first()
    name = row.guild_name if row is not None else None
    return f"**{name}** (`{guild_tag}`)" if name else f"`{guild_tag}`"


def role_phrase(roles: set[str]) -> str:
    """``the **events** and **housing**`` — or ``a``, holding no slots.

    The article changes with the count, so both read as English: "the
    **events** representative" but "a representative".
    """
    if not roles:
        return "a "
    named = [f"**{role}**" for role in sorted(roles)]
    if len(named) == 1:
        joined = named[0]
    else:
        joined = ", ".join(named[:-1]) + f" and {named[-1]}"
    return f"the {joined} "


async def joined(
    member: discord.Member, guild_tag: str, roles: set[str]
) -> bool:
    """Announce a new representative. Returns whether anything was sent."""
    where = await guild_label(guild_tag)
    return await _say(
        member.guild,
        f"{member.mention} joined as {role_phrase(roles)}representative of {where}.",
    )


async def joined_as_observer(member: discord.Member) -> bool:
    """Announce a new observer.

    Worth saying, and worth saying *differently*: an observer represents
    nobody, and a line that read like a representative's would have the
    room looking for a guild they don't have.
    """
    return await _say(
        member.guild,
        f"{member.mention} joined as an observer — they represent no guild.",
    )


async def _say(discord_guild: discord.Guild, body: str) -> bool:
    where = channel(discord_guild)
    if where is None:
        return False
    try:
        await where.send(body, allowed_mentions=SILENT)
    except discord.HTTPException:
        logger.exception("announce: couldn't post %r", body)
        return False
    return True
