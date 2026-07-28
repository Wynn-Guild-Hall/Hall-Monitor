"""``~expel_motion`` — put a guild's removal from the Hall to the delegates.

The rules of the vote live in ``services/expel_motion.py``; this is the
Discord surface for them. Three pieces:

- **A confirmation step.** The command doesn't post anything on its own.
  It DMs the mover what a motion actually does — every delegate notified,
  a guild removed if it carries — with a button to go ahead. A motion is
  the loudest thing anyone in this server can do, and a typo'd tag
  shouldn't be enough to start one. When the mover's DMs are closed the
  warning is posted in the invoking channel instead, because refusing
  outright would make the command unusable for a large share of people
  for a reason that has nothing to do with them.
- **A persistent ballot.** The two buttons carry fixed ``custom_id``s and
  the *message* identifies which motion they belong to, so one registered
  view serves every motion — including ones opened before the last
  restart. Per-motion custom_ids would need re-registering on boot, and a
  motion whose buttons went dead after a deploy would be indistinguishable
  from a bot that had stopped working.
- **A live post.** Every vote edits the message: turnout and the bar, never
  the split. When the motion resolves the message is rewritten with the
  final numbers and the buttons come off.

Voting replies **ephemerally**, always, whatever the outcome. A button
that does nothing visible is the same to the voter as a bot that's down.
"""

import json
import logging

import discord
from discord.ext import commands

from hall_monitor.config import settings
from hall_monitor.db.models import ExpelMotion
from hall_monitor.discord_bot.permissions import is_delegate
from hall_monitor.services import (
    delegate_registry,
    expel_motion,
    roster,
)

logger = logging.getLogger(__name__)

YAY_ID = "hall-monitor:expel:yay"
NAY_ID = "hall-monitor:expel:nay"

CONFIRM_TIMEOUT_SECONDS = 180.0


def _warning(guild_tag: str, moving_for: str, seated: int) -> str:
    """What the mover is told before anything is posted."""
    return "\n".join(
        [
            f"You're about to move that **`{guild_tag}`** be expelled from the "
            f"Guild Hall, on behalf of `{moving_for}`.",
            "",
            "If you confirm:",
            f"· every one of the **{seated}** guilds seated in the Hall is "
            "asked to vote, in the notifications channel;",
            f"· if **{expel_motion.THRESHOLD_PERCENT}%** of them vote yay, "
            f"`{guild_tag}`'s representatives are removed from the server and "
            "the guild is barred from rejoining;",
            f"· the motion closes on its own after "
            f"{settings.expel_motion_days} days if it doesn't reach that.",
            "",
            "Your guild is named on the motion. Individual votes are not.",
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
    @is_delegate()
    async def expel_motion_command(
        self, ctx: commands.Context, guild_tag: str
    ) -> None:
        """move that a guild be expelled from the Hall"""
        if ctx.guild is None:
            await ctx.reply("run this in the server — a motion is put to the Hall.")
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
            guild_tag = await expel_motion.check_can_open(ctx.guild, mover, guild_tag)
        except expel_motion.MotionRejected as exc:
            await ctx.reply(str(exc))
            return

        moving_for = await delegate_registry.represented_guild(mover)
        seated = len(await expel_motion.electorate(ctx.guild, exclude=guild_tag))
        view = ConfirmMotion(self, ctx.author.id, guild_tag)
        warning = _warning(guild_tag, moving_for, seated)

        try:
            view.message = await ctx.author.send(warning, view=view)
        except discord.Forbidden:
            # DMs closed. Posting it here is worse — the tag is now public
            # before anyone voted — but refusing would make the command
            # unusable for a large share of people, so say what happened
            # and let them decide.
            view.message = await ctx.reply(
                f"{warning}\n\n_(I couldn't DM you, so this is here instead.)_",
                view=view,
            )
            return
        await ctx.reply(
            "I've DM'd you what a motion does, with a button to go ahead. "
            "Nothing is posted until you press it."
        )

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

        content = expel_motion.render_open(motion, standing, name)
        mention = _delegate_mention(guild)
        try:
            message = await channel.send(
                f"{mention}{content}" if mention else content,
                view=ExpelBallot(),
                # The one place in this bot that deliberately notifies.
                # The roster is redrawn hourly and must never ping; a
                # motion is a rare, discrete thing every delegate has to
                # act on, and one nobody saw is the same as none at all.
                allowed_mentions=discord.AllowedMentions(roles=True),
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


def _delegate_mention(guild: discord.Guild) -> str:
    """The delegate role, if it's configured and present. Blank otherwise."""
    if not settings.delegate_role_id:
        return ""
    role = guild.get_role(settings.delegate_role_id)
    return f"{role.mention}\n" if role is not None else ""


async def setup(bot: commands.Bot) -> None:
    # Registered before connect, so a motion opened last week still has
    # working buttons after a deploy.
    bot.add_view(ExpelBallot())
    await bot.add_cog(Expel(bot))
