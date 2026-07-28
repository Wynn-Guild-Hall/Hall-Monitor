"""``~script motions`` — what expel motions are open, and where they stand.

Read-only. It reports; it doesn't settle anything (`~script reconcile`
does that, along with everything else the hourly job settles).

**It shows turnout, not the split, and never the mover** — exactly what
the public post shows, even to a monitor. Anonymity that a staff command
quietly opts out of isn't a property anyone can rely on, and "the bot
never says who moved this or who voted which way" has to be true of every
screen or it isn't true. Both are on the rows for auditing; neither is
rendered anywhere.

The one thing here that isn't on the public post is the **electorate
itself**: which guilds are seated and therefore entitled to vote. That's
the number a motion's fate actually turns on, and until now nothing could
answer why a motion was stuck at "needs 7" when the Hall felt smaller
than that.
"""

from hall_monitor.db.models import ExpelBan, ExpelMotion
from hall_monitor.services import expel_motion


async def main(ctx, *args: str) -> None:
    if ctx.guild is None:
        await ctx.reply("run this in the server — it reads the Hall's membership.")
        return

    seated = await expel_motion.seated_guilds(ctx.guild)
    lines = [
        f"**Seated in the Hall — {len(seated)} guild(s)**",
        ", ".join(f"`{tag}`" for tag in sorted(seated.values())) or "_nobody_",
        "",
    ]

    open_motions = await ExpelMotion.filter(state=expel_motion.OPEN).order_by(
        "created_at"
    )
    if not open_motions:
        lines.append("**No open motions.**")
    for motion in open_motions:
        voters = await expel_motion.electorate(ctx.guild, exclude=motion.guild_tag)
        standing = await expel_motion.tally(motion, voters)
        closes = int(expel_motion.deadline(motion).timestamp())
        called = "called" if motion.announced_at is not None else "not called"
        lines += [
            f"**Motion against `{motion.guild_tag}`**",
            f"· {standing.voted} of {standing.electorate} voted · "
            f"{standing.needed} yay needed · closes <t:{closes}:R>",
            f"· Hall {called} to it · post: {_link(ctx, motion)}",
        ]

    banned = await ExpelBan.all().order_by("guild_tag")
    if banned:
        lines += [
            "",
            "**Barred from the Hall**",
            *(f"· `{row.guild_tag}` — {row.reason or 'no reason recorded'}" for row in banned),
        ]

    await ctx.reply("\n".join(lines))


def _link(ctx, motion: ExpelMotion) -> str:
    if motion.discord_channel_id is None or motion.discord_message_id is None:
        return "_never posted_"
    return (
        f"https://discord.com/channels/{ctx.guild.id}/"
        f"{motion.discord_channel_id}/{motion.discord_message_id}"
    )
