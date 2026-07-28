"""Finding the member somebody meant.

discord.py's ``MemberConverter`` matches username, nickname and global
name **exactly**, and in this server that fails on the two names anybody
would actually type:

- **The nickname is ours.** Every representative wears ``Name [TAG]``
  (DESIGN.md §13), so typing ``Struppi0508`` never equals the nickname
  ``Struppi0508 [Volc]`` — the Hall's own feature defeating the lookup.
- **Usernames are lowercase.** Discord's newer handles are case-folded,
  so ``Struppi0508`` doesn't equal ``struppi0508`` either.

Between them, a perfectly correct command reported as a usage error, and
the only thing that reliably worked was a mention.

**The Hall knows these people better than Discord does.** The name in
that nickname prefix is their *Minecraft* username, which is what the
room calls them and what anybody will type — and it's already on the
``Delegate`` row. So that's the lookup that matters here, not a fallback
bolted on after the Discord ones.

Order, and the reason for it:

1. **A mention or a raw ID**, resolved straight from the member cache.
   Unambiguous and free, so it goes first and nothing that works today
   stops working.
2. **Minecraft username**, from ``Delegate`` and ``Observer``. The name
   the Hall uses.
3. **Nickname with our ``[TAG]`` suffix stripped**, then username and
   global name, all case-insensitively — for somebody who copied a
   display name out of the member list.
4. **discord.py's own ``MemberConverter``**, last. Its name path makes a
   **gateway request** (``guild.query_members``) to search members the
   local cache doesn't hold, which is worth having as a backstop and
   worth not doing first: in this server it's slower than the three
   above and less likely to succeed than any of them.

Two matches is refused, not guessed. Assigning a contact slot to the
wrong person is quiet and annoying to undo, and the operator is right
there to disambiguate with a mention.
"""

import logging
import re
from typing import Annotated

import discord
from discord.ext import commands

from hall_monitor.db.models import Delegate, Observer
from hall_monitor.services import nicknames

logger = logging.getLogger(__name__)


class HallMember(commands.Converter):
    """A ``discord.Member`` resolved the way this server names people."""

    async def convert(
        self, ctx: commands.Context, argument: str
    ) -> discord.Member:
        if ctx.guild is None:
            raise commands.BadArgument(
                "I can only look members up inside the server."
            )

        pointed_at = _explicit_id(argument)
        if pointed_at is not None:
            # An explicit mention or ID names one person and no other. If
            # they aren't here, that's the answer — falling through to a
            # name search would go looking for somebody else entirely.
            member = ctx.guild.get_member(pointed_at)
            if member is None:
                raise commands.BadArgument(
                    f"`{argument}` isn't a member of this server."
                )
            return member

        needle = _clean(argument)
        if not needle:
            raise commands.BadArgument("give me a name, a mention or an ID.")

        found = await _by_minecraft_name(ctx.guild, needle)
        found = found or _by_display_name(ctx.guild, needle)

        if not found:
            # Last, because this one goes to the gateway. It searches
            # members the local cache doesn't hold, which nothing above
            # can do.
            #
            # Anything it raises is swallowed, not just `MemberNotFound`:
            # it's a best-effort extra, and a rate limit or a timeout
            # inside it must not turn "I couldn't find them" into a
            # traceback the operator has to decode.
            try:
                return await commands.MemberConverter().convert(ctx, argument)
            except commands.MemberNotFound:
                pass
            except Exception:  # noqa: BLE001 — a nicety must not raise
                logger.warning(
                    "converters: the gateway member search failed for %r; "
                    "falling back to what the cache knows",
                    argument,
                    exc_info=True,
                )
            raise commands.BadArgument(
                f"I couldn't find anybody called `{argument}` — try their "
                "Minecraft name, or @mention them."
            )
        if len(found) > 1:
            listed = ", ".join(sorted(f"`{m}`" for m in found))
            raise commands.BadArgument(
                f"`{argument}` matches more than one member ({listed}) — "
                "@mention the one you meant."
            )
        return found[0]


# A mention is unambiguous whatever it contains, so any digits will do.
# A *bare* number has to look like a snowflake, or a member whose name is
# "12" could never be found by it.
_MENTION = re.compile(r"^<@!?(\d+)>$")
_SNOWFLAKE = re.compile(r"^(\d{15,20})$")


def _explicit_id(argument: str) -> int | None:
    """The user ID a mention or raw ID names, if it is one.

    Read here rather than left to ``MemberConverter`` so the unambiguous
    case costs nothing: no gateway request, and no chance of a name
    search running first and landing on somebody else.
    """
    text = argument.strip()
    match = _MENTION.match(text) or _SNOWFLAKE.match(text)
    return int(match.group(1)) if match else None


def _clean(argument: str) -> str:
    """Strip the decorations a pasted name arrives with.

    ``@Struppi0508 [Volc]`` is what you get from copying a display name
    out of the member list, and it's a reasonable thing to paste.
    """
    return nicknames.visible_part(argument.strip().lstrip("@").strip())


async def _by_minecraft_name(
    guild: discord.Guild, needle: str
) -> list[discord.Member]:
    """Members whose Minecraft username matches, representatives first.

    The name the Hall knows them by, and the one in their nickname
    prefix. Observers are searched too — they have a Minecraft account on
    file for exactly this sort of question (§18.1).
    """
    ids = set()
    for model in (Delegate, Observer):
        rows = await model.filter(mc_username__iexact=needle).values(
            "discord_user_id"
        )
        ids.update(row["discord_user_id"] for row in rows)
    return [member for member in map(guild.get_member, ids) if member is not None]


def _by_display_name(guild: discord.Guild, needle: str) -> list[discord.Member]:
    """Members whose nickname, username or global name matches, folded.

    The nickname is compared with our ``[TAG]`` suffix removed, since
    that suffix is the thing standing between a typed name and a match.
    """
    folded = needle.casefold()
    return [
        member
        for member in guild.members
        if folded
        in {
            nicknames.visible_part(member.nick or "").casefold(),
            (member.name or "").casefold(),
            (member.global_name or "").casefold(),
        }
        - {""}
    ]


# What a command parameter should be annotated with. `Annotated` keeps
# the declared type honest for a reader and for a type-checker — these
# handlers really do receive a `discord.Member` — while telling
# discord.py which converter to run.
HallMemberArg = Annotated[discord.Member, HallMember]
