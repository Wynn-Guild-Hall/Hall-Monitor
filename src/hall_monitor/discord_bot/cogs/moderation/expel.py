"""``~expel_motion`` — put a guild's removal from the Hall to the delegates.

The rules of the vote live in ``services/expel_motion.py``; this is the
Discord surface for them. Four pieces:

- **DM-only, because the mover is anonymous.** Running ``~expel_motion``
  in a channel names you to everyone in it, which no amount of care
  inside the bot can undo — so the command refuses there, deletes the
  message if it can, and says to try again in a DM. Anonymity you have to
  remember to use isn't anonymity.
- **A confirmation step.** The command posts nothing on its own. It
  answers with what a motion actually does — a public vote, a guild
  removed if it carries — and a button to go ahead. A typo'd tag
  shouldn't be enough to start one.
- **A persistent ballot.** The two buttons carry fixed ``custom_id``s and
  the *message* identifies which motion they belong to, so one registered
  view serves every motion — including ones opened before the last
  restart. Per-motion custom_ids would need re-registering on boot, and a
  motion whose buttons went dead after a deploy would be indistinguishable
  from a bot that had stopped working.
- **A live post**, edited on every vote: turnout and the bar, never the
  split, never the mover. When the motion resolves the message is
  rewritten with the final numbers and the buttons come off.

The motion post itself **notifies nobody**. One person deciding to ping
the server is not a thing this bot does — people leave over stray pings.
The Hall is called to a vote exactly once, with an ``@here`` in the
delegate channel, and only once ``ANNOUNCE_AT_YAY`` guilds are already
behind it: at that point it's the Hall's business rather than one
member's.

Voting replies **ephemerally**, always, whatever the outcome. A button
that does nothing visible is the same to the voter as a bot that's down.
"""

import json
import logging

import discord
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.db.models import ExpelMotion
from hall_monitor.discord_bot.permissions import (
    CONTACT_ROLE_ATTRS,
    STAFF_ROLE_ATTRS,
    has_any_role,
)
from hall_monitor.services import (
    delegate_registry,
    expel_motion,
    roster,
)

logger = logging.getLogger(__name__)

YAY_ID = "hall-monitor:expel:yay"
NAY_ID = "hall-monitor:expel:nay"

CONFIRM_TIMEOUT_SECONDS = 180.0


def may_move():
    """``is_delegate`` where there are roles to read, the DB where there aren't.

    A DM has no member and no roles, so the usual gate would refuse every
    invocation of a command that is deliberately DM-only. The authoritative
    check is ``expel_motion.check_can_open`` anyway — it asks whether they
    hold a *seat*, which is stricter than holding the role — so the gate
    here exists only to keep the in-server ``~help`` listing honest.
    """

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return True
        wanted = ("delegate_role_id", *CONTACT_ROLE_ATTRS, *STAFF_ROLE_ATTRS)
        return has_any_role(ctx, *(getattr(settings, name) for name in wanted))

    return commands.check(predicate)


def _warning(guild_tag: str, moving_for: str, seated: int) -> str:
    """What the mover is told before anything is posted."""
    return "\n".join(
        [
            f"You're about to move that **`{guild_tag}`** be expelled from the "
            f"Guild Hall, on behalf of `{moving_for}`.",
            "",
            "If you confirm:",
            f"· a vote goes up in the notifications channel for the "
            f"**{seated}** guilds seated in the Hall;",
            f"· if **{expel_motion.THRESHOLD_PERCENT}%** of them vote yay, "
            f"`{guild_tag}`'s representatives are removed from the server and "
            "the guild is barred from rejoining;",
            f"· the motion closes on its own after "
            f"{settings.expel_motion_days} days if it doesn't reach that.",
            "",
            "**Nothing names you or your guild** — not the post, not the "
            "result, not the staff tools. Individual votes are private too. "
            f"Nobody is pinged unless {expel_motion.ANNOUNCE_AT_YAY} guilds "
            "vote yay, at which point the Hall gets one `@here` about it.",
        ]
    )


class ExpelBallot(discord.ui.View):
    """The yay/nay buttons under a motion. One instance serves every motion."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Yay — expel", style=discord.ButtonStyle.danger, custom_id=YAY_ID
    )
    async def yay(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._vote(interaction, yay=True)

    @discord.ui.button(
        label="Nay — keep", style=discord.ButtonStyle.secondary, custom_id=NAY_ID
    )
    async def nay(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._vote(interaction, yay=False)

    async def _vote(self, interaction: discord.Interaction, *, yay: bool) -> None:
        # Deferred first: casting a vote re-reads the electorate, which is
        # a walk over every delegate, and Discord gives an interaction
        # three seconds before it calls the whole thing failed.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._record(interaction, yay=yay)
        except Exception:  # noqa: BLE001 — a button has nobody to raise to
            logger.exception("expel: vote from %s failed", interaction.user.id)
            await interaction.followup.send(
                "that broke on my end — the details are in my logs.", ephemeral=True
            )

    async def _record(self, interaction: discord.Interaction, *, yay: bool) -> None:
        motion = await ExpelMotion.get_or_none(
            discord_message_id=interaction.message.id
        )
        if motion is None or interaction.guild is None:
            await interaction.followup.send(
                "I've no record of this vote — it's from before my time. "
                "Ask a monitor to open a fresh motion.",
                ephemeral=True,
            )
            return

        outcome = await expel_motion.cast_vote(
            interaction.guild, motion, interaction.user.id, yay=yay
        )
        await interaction.followup.send(outcome.message, ephemeral=True)
        if not outcome.recorded:
            return

        # A vote can be the one that carries it, so settle before redrawing
        # — otherwise the post would show a turnout for a motion that has
        # already resolved, and the next reader would press a dead button.
        await expel_motion.settle(interaction.guild, motion)
        await refresh(interaction.client, motion)
        # And it can be the one that makes the motion the Hall's business
        # rather than one member's. No-ops unless it just crossed the line.
        await announce_if_ready(interaction.client, interaction.guild, motion)


class ConfirmMotion(discord.ui.View):
    """One-shot confirmation, bound to the delegate who ran the command."""

    def __init__(self, cog: "Expel", mover_id: int, guild_tag: str) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT_SECONDS)
        self.cog = cog
        self.mover_id = mover_id
        self.guild_tag = guild_tag
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the mover may press it.

        Matters for the fallback path: when DMs are closed the warning is
        posted in a public channel, where without this anyone passing by
        could start a motion in somebody else's name.
        """
        if interaction.user.id == self.mover_id:
            return True
        await interaction.response.send_message(
            "that's not your motion to confirm.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Move it", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        await self._finish()
        try:
            await self.cog.post_motion(self.mover_id, self.guild_tag, interaction)
        except Exception:  # noqa: BLE001 — a button has nobody to raise to
            logger.exception("expel: couldn't post the motion against %s", self.guild_tag)
            await interaction.followup.send(
                "that broke on my end — nothing was posted. The details are in "
                "my logs.",
                ephemeral=True,
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        await self._finish()
        await interaction.followup.send("dropped it — nothing was posted.")

    async def on_timeout(self) -> None:
        """A confirmation left unanswered lapses rather than staying armed."""
        await self._finish()

    async def _finish(self) -> None:
        """Take the buttons off, so a stale prompt can't be pressed twice."""
        self.stop()
        if self.message is None:
            return
        try:
            await self.message.edit(view=None)
        except discord.HTTPException:
            logger.debug("expel: couldn't clear the confirm buttons", exc_info=True)


async def refresh(bot: discord.Client, motion: ExpelMotion) -> None:
    """Rewrite a motion's post to match its current state.

    Best-effort, like the roster: a channel edit that fails must not undo
    a vote that was recorded, and the next vote or the hourly pass will
    redraw it anyway.
    """
    if motion.discord_channel_id is None or motion.discord_message_id is None:
        return
    channel = bot.get_channel(motion.discord_channel_id)
    if channel is None:
        logger.warning(
            "expel: motion channel %s is gone; can't update the post",
            motion.discord_channel_id,
        )
        return
    try:
        message = await channel.fetch_message(motion.discord_message_id)
    except discord.HTTPException:
        logger.warning(
            "expel: motion message %s is gone; leaving it",
            motion.discord_message_id,
            exc_info=True,
        )
        return

    guild = getattr(channel, "guild", None)
    name = await expel_motion.guild_name(motion.guild_tag)
    if motion.state == expel_motion.OPEN and guild is not None:
        voters = await expel_motion.electorate(guild, exclude=motion.guild_tag)
        standing = await expel_motion.tally(motion, voters)
        content = expel_motion.render_open(motion, standing, name)
        view: discord.ui.View | None = ExpelBallot()
    else:
        standing = _recorded_tally(motion)
        content = expel_motion.render_resolved(motion, standing, name)
        view = None  # a closed motion keeps no live buttons

    try:
        await message.edit(content=content, view=view, allowed_mentions=roster.SILENT)
    except discord.HTTPException:
        logger.exception("expel: couldn't update the motion post for %s", motion.guild_tag)


def _recorded_tally(motion: ExpelMotion) -> expel_motion.Tally:
    """The split as it stood when the motion closed, not as it stands now.

    Recomputing would answer with today's electorate, which is a different
    question from the one the Hall settled.
    """
    try:
        recorded = json.loads(motion.tally_json or "{}")
    except ValueError:
        recorded = {}
    return expel_motion.Tally(
        electorate=int(recorded.get("electorate", 0)),
        yay=int(recorded.get("yay", 0)),
        nay=int(recorded.get("nay", 0)),
    )


class Expel(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="expel_motion")
    @may_move()
    async def expel_motion_command(
        self, ctx: commands.Context, guild_tag: str
    ) -> None:
        """move that a guild be expelled from the Hall (DM me)"""
        if ctx.guild is not None:
            await self._redirect_to_dm(ctx)
            return
        guild = self.bot.get_guild(settings.discord_guild_id)
        if guild is None:
            await ctx.reply(
                "I can't see the Guild Hall server at the moment — try again "
                "in a minute."
            )
            return
        if not settings.notifications_channel_id:
            await ctx.reply(
                "there's no notifications channel configured, so I've nowhere "
                "to put a motion where the Hall would see it. That's a deploy "
                "setting — ask a monitor."
            )
            return

        mover = await delegate_registry.get_by_discord_user_id(ctx.author.id)
        try:
            # Returns the Hall's own spelling of the tag, so the motion is
            # recorded as `OTHR` however the mover typed it.
            guild_tag = await expel_motion.check_can_open(guild, mover, guild_tag)
        except expel_motion.MotionRejected as exc:
            await ctx.reply(str(exc))
            return

        moving_for = await delegate_registry.represented_guild(mover)
        seated = len(await expel_motion.electorate(guild, exclude=guild_tag))
        view = ConfirmMotion(self, ctx.author.id, guild_tag)
        view.message = await ctx.reply(
            _warning(guild_tag, moving_for, seated), view=view
        )

    async def _redirect_to_dm(self, ctx: commands.Context) -> None:
        """Refuse a public invocation, and clean it up if we're allowed to.

        The message itself is the leak — it names the mover to everyone
        who can read the channel, and nothing the bot does afterwards
        takes that back. So it goes if we have Manage Messages there, and
        the explanation is DM'd rather than posted, since a reply saying
        "motions are anonymous" under a visible one would only draw eyes
        to it.
        """
        deleted = True
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            deleted = False
            logger.warning(
                "expel: couldn't delete a public ~expel_motion from %s "
                "(needs Manage Messages in that channel)",
                ctx.author.id,
            )
        tail = (
            "I've removed your message."
            if deleted
            else "**Your message is still visible — delete it yourself.**"
        )
        note = (
            "Motions are anonymous, and running that in a channel names you "
            f"to everyone in it. Send it to me here instead. {tail}"
        )
        try:
            await ctx.author.send(note)
        except discord.Forbidden:
            # Nowhere private left to answer. Better a visible refusal than
            # silence, which reads as the bot being broken.
            await ctx.send(f"{ctx.author.mention} {note}", delete_after=30)

    async def post_motion(
        self, mover_id: int, guild_tag: str, interaction: discord.Interaction
    ) -> None:
        """Create the motion and put it to the Hall.

        Re-checks everything the command checked. Minutes can pass on the
        confirmation, and in that time the guild can be banned by a
        monitor, another motion can open, or the mover's own guild can
        drop out of the Hall — all of which the first check would have
        stopped and none of which it can see.
        """
        guild = self.bot.get_guild(settings.discord_guild_id)
        channel = (
            guild.get_channel(settings.notifications_channel_id)
            if guild is not None
            else None
        )
        if guild is None or channel is None:
            await interaction.followup.send(
                "I can't reach the notifications channel any more — nothing "
                "was posted."
            )
            return

        mover = await delegate_registry.get_by_discord_user_id(mover_id)
        try:
            guild_tag = await expel_motion.check_can_open(guild, mover, guild_tag)
        except expel_motion.MotionRejected as exc:
            await interaction.followup.send(f"nothing posted — {exc}")
            return

        moving_for = await delegate_registry.represented_guild(mover)
        motion = await expel_motion.open_motion(mover, moving_for, guild_tag)
        voters = await expel_motion.electorate(guild, exclude=guild_tag)
        standing = await expel_motion.tally(motion, voters)
        name = await expel_motion.guild_name(guild_tag)

        try:
            message = await channel.send(
                expel_motion.render_open(motion, standing, name),
                view=ExpelBallot(),
                # Notifies nobody. One member deciding to ping the server
                # is not something this bot lets happen — the Hall is
                # called to a vote only once other guilds are behind it,
                # by `announce_if_ready`.
                allowed_mentions=roster.SILENT,
            )
        except discord.HTTPException:
            await motion.delete()
            logger.exception("expel: couldn't post the motion against %s", guild_tag)
            await interaction.followup.send(
                "I couldn't post to the notifications channel, so the motion "
                "was dropped rather than left half-open."
            )
            return

        motion.discord_channel_id = channel.id
        motion.discord_message_id = message.id
        await motion.save(update_fields=["discord_channel_id", "discord_message_id"])
        logger.info(
            "expel: %s moved to expel %s on behalf of %s (%d guild(s) voting)",
            mover_id,
            guild_tag,
            moving_for,
            standing.electorate,
        )
        await interaction.followup.send(
            f"posted — the Hall is voting on `{guild_tag}` in {channel.mention}."
        )


async def announce_if_ready(
    bot: discord.Client, guild: discord.Guild, motion: ExpelMotion
) -> bool:
    """Call the Hall to a motion enough guilds are already behind.

    The **only** `@here` this bot sends, once per motion ever. A ping is
    borrowed attention and people leave servers over stray ones, so it
    has to be the Hall's business rather than one member's — which is
    what `ANNOUNCE_AT_YAY` establishes — and a motion that has already
    carried or lapsed doesn't send one at all.

    Best-effort and idempotent: the row is stamped only once the message
    is actually out, so a failed send is retried by the next pass rather
    than silently swallowing the one announcement a motion gets.
    """
    voters = await expel_motion.electorate(guild, exclude=motion.guild_tag)
    standing = await expel_motion.tally(motion, voters)
    if not expel_motion.should_announce(motion, standing):
        return False
    if not settings.delegate_channel_id:
        logger.warning(
            "expel: %s has %d yay but DELEGATE_CHANNEL_ID is unset; the Hall "
            "won't be called to it",
            motion.guild_tag,
            standing.yay,
        )
        return False
    channel = guild.get_channel(settings.delegate_channel_id)
    if channel is None:
        logger.warning(
            "expel: delegate channel %s is not in the guild; can't call the "
            "Hall to the motion against %s",
            settings.delegate_channel_id,
            motion.guild_tag,
        )
        return False

    name = await expel_motion.guild_name(motion.guild_tag)
    body = expel_motion.render_call(motion, standing, name)
    link = _link(guild, motion)
    try:
        await channel.send(
            f"{body}\n{link}" if link else body,
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
    except discord.HTTPException:
        logger.exception(
            "expel: couldn't call the Hall to the motion against %s",
            motion.guild_tag,
        )
        return False
    await expel_motion.mark_announced(motion)
    logger.info(
        "expel: called the Hall to the motion against %s (%d yay of %d seated)",
        motion.guild_tag,
        standing.yay,
        standing.electorate,
    )
    return True


async def sync_all(bot: discord.Client, guild: discord.Guild) -> None:
    """Settle, redraw and announce every motion. The hourly pass.

    Resolution runs here as well as on each button because the electorate
    moves on its own (DESIGN.md §16.2), and the announcement does because
    a send that failed gets exactly one more chance per hour rather than
    none.
    """
    for resolution in await expel_motion.resolve_open(guild):
        await refresh(bot, resolution.motion)
    for motion in await ExpelMotion.filter(state=expel_motion.OPEN):
        await refresh(bot, motion)
        await announce_if_ready(bot, guild, motion)


def _link(guild: discord.Guild, motion: ExpelMotion) -> str:
    if motion.discord_channel_id is None or motion.discord_message_id is None:
        return ""
    return (
        f"https://discord.com/channels/{guild.id}/"
        f"{motion.discord_channel_id}/{motion.discord_message_id}"
    )


async def setup(bot: commands.Bot) -> None:
    # Registered before connect, so a motion opened last week still has
    # working buttons after a deploy.
    bot.add_view(ExpelBallot())
    await bot.add_cog(Expel(bot))
