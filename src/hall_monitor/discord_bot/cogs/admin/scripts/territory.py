"""``~script territory [TAG]`` — who is actually *holding* territory.

Signal 4 asks whether a guild kept more than twenty territories across
five days, which is a claim about a series and can't be read off a
current count. This shows the series: how long we've been watching, and
what share of those readings each guild spent above the bar.

With a guild tag, it reports that one guild's record and says plainly
whether it qualifies and why not.

Reads the samples the sweep records, so it costs no API call. Note that
the signal stays false for everyone until the history spans the full
window — five days after this first ran.
"""

from hall_monitor.services import guild_tag as tags, major_guilds, territory_history


async def main(ctx, *args: str) -> None:
    window = await territory_history.load(major_guilds.MIN_TERRITORIES)

    if not window.sweeps:
        await ctx.reply(
            "no territory readings yet — they're recorded by the major-guild "
            "sweep, so run `~script refresh_major` or wait for the hour."
        )
        return

    watched = window.watched.total_seconds() / 86400
    state = (
        f"watching for {watched:.1f}d of the {territory_history.WINDOW.days}d "
        "window — **the territory signal reads false for everyone until this "
        "is covered**"
        if not window.covered
        else f"{watched:.1f}d of history, window covered"
    )

    if args:
        tag = args[0]
        holds = window.sustained(tag)
        await ctx.reply(
            f"**`{tag}`** — territory signal: {'**yes**' if holds else 'no'}\n"
            f"{window.line(tag)}\n"
            f"_{window.sweeps} readings, {state}. Needs "
            f"{territory_history.SUSTAINED_FRACTION:.0%} of readings above "
            f"{major_guilds.MIN_TERRITORIES}._"
        )
        return

    ranked = sorted(
        window.holdings.items(),
        key=lambda item: (-item[1].above, item[0]),
    )
    rows = [
        f"{tag[:8].ljust(8)} {holding.above:>4}/{window.sweeps:<4} "
        f"{window.fraction(tag):>5.0%}  {holding.low:>3}–{holding.high:<3} "
        f"{'Y' if window.sustained(tag) else '·'}"
        for tag, holding in ranked
        if holding.high > 0
    ]
    if not rows:
        await ctx.reply(f"nobody has held any territory. _{state}._")
        return

    head = f"guild    above/of    %    range  ok"
    body = "\n".join([head, *rows[:40]])
    await ctx.reply(
        f"**Territory held above {major_guilds.MIN_TERRITORIES}** across "
        f"{window.sweeps} readings — {state}.\n```\n{body}\n```"
    )
