"""``~script season [TAG]`` — the season criterion, split into its three rules.

`~script major_guilds` collapses season placement into one column, which
hides the thing worth knowing: the three rules make very different claims.
One outstanding season two years ago and a steady mid-table record both
read as `Y`, and only one of them is a case for tightening.

Without an argument: every guild the season signal fires for, with a
column per rule, and a count of how many rest on each alone.

With a guild tag: that guild's rank in each of the last 10 seasons and
which rules those ranks satisfy.

Reads the cache, so it's instant and costs no API calls — the ranks were
recorded by the last sweep (`~script refresh_major` to freshen).
"""

import json

from hall_monitor.db.models import MajorGuildCache
from hall_monitor.services import guild_tag as tags, major_guilds

# Derived from the bounds rather than written out, so a threshold change
# can't leave the column headings claiming the old one.
_HEADS = {
    major_guilds.PEAK_LAST_10: f"<={major_guilds.SEASON_PEAK_LAST_10}/10",
    major_guilds.PEAK_LAST_5: f"<={major_guilds.SEASON_PEAK_LAST_5}/5",
    major_guilds.MEAN_LAST_5: f"mean<={major_guilds.SEASON_MEAN_LAST_5}",
}


async def _rows():
    """Cached guilds whose season signal fired, with their rule verdicts."""
    rows = []
    for row in await MajorGuildCache.all().values(
        "guild_tag", "guild_name", "signals_json", "metrics_json"
    ):
        try:
            signals = json.loads(row["signals_json"] or "{}")
            metrics = json.loads(row["metrics_json"] or "{}")
        except ValueError:
            continue
        rules = metrics.get("season_rules") or {}
        ranks = metrics.get("season_ranks") or []
        rows.append((row["guild_tag"], row["guild_name"], signals, rules, ranks))
    return rows


async def main(ctx, *args: str) -> None:
    rows = await _rows()
    if not rows:
        await ctx.reply(
            "no major-guild cache — run `~script refresh_major` first."
        )
        return
    if args:
        await _one(ctx, rows, args[0])
        return

    fired = [r for r in rows if r[2].get("season_placement")]
    if not fired:
        await ctx.reply("no guild currently qualifies on season placement.")
        return
    # A rules dict from before a rename carries the old keys, which read
    # as "no rule fired" rather than as a stale cache.
    if not any(set(major_guilds.SEASON_RULES) & set(r[3]) for r in fired):
        await ctx.reply(
            "the cache predates the per-rule breakdown — run "
            "`~script refresh_major` to record it."
        )
        return

    width = max(len(r[0]) for r in fired)
    lines = [
        "guild".ljust(width) + "  " + " ".join(h.rjust(7) for h in _HEADS.values())
    ]
    # Single-rule guilds first: those are the ones a threshold change moves.
    for tag, _name, _signals, rules, _ranks in sorted(
        fired, key=lambda r: (sum(1 for v in r[3].values() if v), r[0].upper())
    ):
        lines.append(
            tag.ljust(width)
            + "  "
            + " ".join(
                ("Y" if rules.get(rule) else "·").rjust(7)
                for rule in major_guilds.SEASON_RULES
            )
        )

    alone = {
        rule: sum(
            1
            for _t, _n, _s, rules, _r in fired
            if rules.get(rule) and sum(1 for v in rules.values() if v) == 1
        )
        for rule in major_guilds.SEASON_RULES
    }
    header = f"**{len(fired)} guilds qualify on season placement.** Carried by one rule alone:\n" + "\n".join(
        f"- `{_HEADS[rule]}` — {alone[rule]} ({major_guilds.SEASON_RULE_LABELS[rule]})"
        for rule in major_guilds.SEASON_RULES
    )
    await ctx.reply(f"{header}\n```\n" + "\n".join(lines) + "\n```")


async def _one(ctx, rows, wanted: str) -> None:
    match = next((r for r in rows if tags.matches(r[0], wanted)), None)
    if match is None:
        await ctx.reply(f"`{wanted}` isn't in the major-guild cache.")
        return
    tag, name, signals, rules, ranks = match

    if not ranks:
        await ctx.reply(
            f"no season record cached for `{tag}` — run "
            "`~script refresh_major` to record it."
        )
        return

    placed = [r for r in ranks[:5] if r is not None]
    mean = (
        f"{sum(placed) / len(placed):.1f}"
        if len(placed) == len(ranks[:5]) and placed
        else f"n/a — placed in {len(placed)} of the last 5"
    )
    shown = ", ".join(str(r) if r else "—" for r in ranks)
    verdicts = "\n".join(
        f"- {'**yes**' if rules.get(rule) else 'no'} — "
        f"{major_guilds.SEASON_RULE_LABELS[rule]}"
        for rule in major_guilds.SEASON_RULES
    )
    await ctx.reply(
        f"**{name or tag}** (`{tag}`) — season placement: "
        f"{'**yes**' if signals.get('season_placement') else 'no'}\n"
        f"ranks, newest season first: `{shown}`\n"
        f"mean over the last 5: `{mean}`\n"
        f"{verdicts}\n"
        "_A dash means outside the top 100 that season, which is not the "
        "same as a bad rank._"
    )
