"""Shared plumbing for the four "say it as the bot" commands.

``~echo`` and ``~embed`` each come in two: a **silent** janitor-tier one
that notifies nobody, and a **noisy** monitor-tier one that may ping
anything including ``@everyone``. Splitting them beats one command
guessing, because the two are wanted in genuinely different situations —
naming a role in a notice is routine, waking three hundred people is not
— and a single command has to either over-notify or under-notify half
the time.

The tiers follow that: pinging the server is a monitor's call
(DESIGN.md §16.4 is the only other place this bot notifies deliberately,
and it takes three guilds agreeing). Everything else is janitor work.
``~echo`` and ``~embed`` alias the silent ones, so the short name anybody
reaches for first is the one that can't wake the room.

The private module name keeps it out of the cog loader, which walks this
package and calls ``setup()`` on anything not starting with ``_``.
"""

import logging

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

# Nothing notifies. Mentions still *render* — `@Guild Hall Delegate`
# looks like itself and links through — they simply don't ring.
SILENT = discord.AllowedMentions.none()

# Everything notifies, `@everyone` included. Monitor-only.
NOISY = discord.AllowedMentions.all()


async def post_then_delete(
    ctx: commands.Context,
    *,
    content: str = "",
    embed: discord.Embed | None = None,
    files: list[discord.File] | None = None,
    mentions: discord.AllowedMentions,
    label: str,
) -> bool:
    """Post as the bot, then remove the invoking message. Returns success.

    **Deleted last, and only once the post is out.** The other order
    loses what somebody has just written whenever the send fails — and
    what these commands carry is usually an announcement that took a few
    minutes to word.

    A delete that fails is logged and the post stands: it needs Manage
    Messages in that channel, and a missing permission shouldn't cost the
    message.
    """
    try:
        await ctx.send(
            content or "",
            embed=embed,
            files=files or [],
            allowed_mentions=mentions,
        )
    except discord.HTTPException as exc:
        logger.exception("%s: couldn't post for %s", label, ctx.author.id)
        # Discord validates embeds server-side — a malformed `url`, an
        # over-long field — and its own complaint is far more use to the
        # author than "that broke on my end" would be.
        detail = getattr(exc, "text", None)
        await ctx.reply(
            f"Discord refused that: {detail}"
            if detail
            else "that broke on my end — the details are in my logs."
        )
        return False

    try:
        await ctx.message.delete()
    except discord.HTTPException:
        logger.warning(
            "%s: posted for %s but couldn't delete their message "
            "(needs Manage Messages here)",
            label,
            ctx.author.id,
        )
    return True
