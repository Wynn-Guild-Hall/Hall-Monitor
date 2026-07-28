"""``~force guild`` — say which guild a member represents.

A janitor asserting who someone speaks for, which outranks both the guild
they verified as a chief of and whatever the hourly watch sees. Everything
follows from that one answer: the tag on their nickname, the colour they
wear, whose contact slots they may hold, and whose major-guild status decides
their standing. Force someone to ANO and they are, for every purpose the
Hall has, an ANO representative.

Two shapes of problem it solves. **Repointing:** somebody now speaks for
a different guild than the one they verified with, and re-verifying isn't
possible while their delegate row is live (DESIGN.md §12.2). **Correcting
the watch:** a rep mid-transfer flickers, an alt account shows the wrong
guild, a shared account shows whoever logged in last — forcing the guild
they already represent pins it and undoes a wrong External Relegate.

The override sits in front of ``Delegate.current_guild_tag`` rather than
in it, because the watch rewrites that column every hour: a forced value
stored there would survive exactly until the next sweep. And a forced
representative is never external, since the watch disagreeing is the
whole situation being overridden.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import discord
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.discord_bot.converters import HallMemberArg
from hall_monitor.discord_bot.permissions import has_any_role, is_janitor
from hall_monitor.services import (
    contacts,
    delegate_registry,
    guild_tag as tags,
    nicknames,
    major_guilds,
    transitions,
)
from hall_monitor.services.time_parse import (
    InvalidDuration,
    gating_rejection,
    parse as parse_duration,
)


@dataclass(frozen=True)
class Applied:
    """What applying an override actually did, for the reply to say out loud."""

    settlement: transitions.Settlement | None
    contacts_changed: int
    nickname_changed: bool

    @property
    def changed(self) -> bool:
        return bool(
            (self.settlement and self.settlement.changes)
            or self.contacts_changed
            or self.nickname_changed
        )

    def line(self) -> str:
        """A sentence naming the end state, or saying nothing moved.

        Both halves matter. Naming the end state means a command that
        settled on something unexpected is visible immediately, and saying
        so when nothing moved means a no-op can't pass for success — the
        failure mode that took three bugs to notice, because the hourly
        reconcile made every one of them look like a delay.
        """
        if self.settlement is None:
            return "They aren't in the server, so there was nothing to apply."
        parts = [self.settlement.line()]
        if self.contacts_changed:
            parts.append(f"{self.contacts_changed} contact role change(s)")
        if self.nickname_changed:
            parts.append("nickname updated")
        state = ", ".join(parts)
        if not self.changed:
            return f"Already {state} — nothing needed changing."
        return f"Now {state}."


async def apply_now(ctx: commands.Context, user: discord.Member) -> Applied:
    """Settle the target's standing, roles and nickname immediately.

    **Both guilds, and in this order.** A repoint is a move: the guild
    they came from has to give up its contact roles, and the one they've
    gone to has to mint its role and hand over the colour and standing.
    Settling only the row's guild does nothing at all once the reconcile
    groups members by who they represent — that pass no longer contains
    them — which is exactly how the first version left someone forced to
    `ANO` with no ANO role and the log showing nothing wrong.

    The hourly reconcile would reach the same state on its own. Waiting an
    hour to find out whether a command did what you meant is what makes an
    override feel broken.
    """
    delegate = await delegate_registry.get_by_discord_user_id(user.id)
    if delegate is None or ctx.guild is None:
        return Applied(settlement=None, contacts_changed=0, nickname_changed=False)

    represents = await delegate_registry.represented_guild(delegate)
    contacts_changed = 0
    for tag in _affected(delegate.guild_tag, represents):
        major = await major_guilds.is_major(tag)
        contacts_changed += await contacts.sync_contact_roles(
            tag, discord_guild=ctx.guild, granted=major
        )
    settlement = await transitions.settle_representative(ctx.guild, delegate)
    renamed = await nicknames.enforce(
        user, reason=f"hall-monitor: ~force guild by {ctx.author}"
    )
    return Applied(
        settlement=settlement,
        contacts_changed=contacts_changed,
        nickname_changed=renamed,
    )


def _affected(*tags_in_order: str) -> list[str]:
    """The guilds to settle, deduplicated, oldest allegiance first."""
    seen: dict[str, str] = {}
    for tag in tags_in_order:
        seen.setdefault(tags.normalise(tag), tag)
    return list(seen.values())


def register(cog: commands.Cog) -> None:
    @cog.force.command(name="guild")
    @is_janitor()
    async def force_guild(
        ctx: commands.Context, user: HallMemberArg, guild_tag: str, duration: str
    ) -> None:
        """say which guild a member represents, whatever Wynncraft says"""
        try:
            delta = parse_duration(duration)
        except InvalidDuration:
            await ctx.reply(
                f"couldn't parse `{duration}` — expected e.g. `3mo`, `7d`, "
                "or `0` (monitor only)."
            )
            return

        rejection = gating_rejection(
            delta, has_any_role(ctx, settings.monitor_role_id)
        )
        if rejection is not None:
            await ctx.reply(rejection)
            return

        expires_at = None if delta is None else datetime.now(timezone.utc) + delta
        await delegate_registry.set_forced_guild(user.id, guild_tag, expires_at)
        applied = await apply_now(ctx, user)

        delegate = await delegate_registry.get_by_discord_user_id(user.id)
        window = (
            "permanently"
            if expires_at is None
            else f"until {expires_at.isoformat(timespec='minutes')}"
        )
        note = (
            ""
            if delegate is not None
            else " They aren't a registered delegate, so nothing changes for "
            "them until they verify."
        )
        await ctx.reply(
            f"{user.mention} represents `{guild_tag}` {window}. "
            f"{applied.line()}{note}"
        )

    @cog.unforce.command(name="guild")
    @is_janitor()
    async def unforce_guild(ctx: commands.Context, user: discord.Member) -> None:
        """drop a guild override and go back to what Wynncraft says"""
        cleared = await delegate_registry.clear_forced_guild(user.id)
        if not cleared:
            await ctx.reply(f"no guild override on {user.mention} to clear.")
            return
        applied = await apply_now(ctx, user)
        await ctx.reply(
            f"cleared the guild override on {user.mention} — back to the guild "
            f"they verified with. {applied.line()}"
        )
