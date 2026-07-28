"""``~script major_breakdown`` — which criteria are producing major guilds.

Reads the cache rather than re-evaluating, so it's instant and costs no
API calls. Run ``~script refresh_major`` first if the numbers look
stale.

The column worth reading is **only**: guilds that signal alone made
major. A signal with a high *any* but a low *only* is corroborating
other signals; a signal with a high *only* is the one setting the bar,
and the one to tighten.

**skipped** should read zero: every signal now comes from a bulk
leaderboard, so all six are evaluated for every guild. A non-zero count
means a board stopped covering our threshold and the per-guild fallback
couldn't fill the gap — check the logs for a `guildWars`/`guildTerritories`
warning.

``~script major_breakdown <signal>`` lists the guilds a given signal
is solely responsible for.
"""

from . import _signal_rows

_MAX_LISTED = 60


async def main(ctx, *args: str) -> None:
    rows = await _signal_rows.load()
    if not rows:
        await ctx.reply(
            "no major-guild cache rows yet — run `~script refresh_major` first."
        )
        return

    qualified = [row for row in rows if row.major]
    major = [(row.tag, row.met) for row in qualified]
    skipped_by = [row.skipped for row in qualified]
    names = _signal_rows.columns(qualified)

    if args:
        await _list_sole_cause(ctx, major, names, args[0])
        return

    counts = []
    for name in names:
        any_count = sum(1 for _tag, met in major if name in met)
        only_count = sum(1 for _tag, met in major if met == {name})
        skipped = sum(1 for unasked in skipped_by if name in unasked)
        counts.append((name, any_count, only_count, skipped))
    # Most-responsible first: that's the criterion to tighten.
    counts.sort(key=lambda row: (-row[2], -row[1], row[0]))

    width = max(len(name) for name, *_rest in counts)
    lines = [f"{'signal'.ljust(width)}   any   only  skipped"]
    lines += [
        f"{name.ljust(width)} {any_count:>5} {only_count:>6} {skipped:>8}"
        for name, any_count, only_count, skipped in counts
    ]

    sole = sum(1 for _tag, met in major if len(met) == 1)
    several = sum(1 for _tag, met in major if len(met) > 1)
    unexplained = sum(1 for _tag, met in major if not met)

    body = "\n".join(lines)
    summary = f"sole cause: {sole} · multiple: {several} · unexplained: {unexplained}"
    await ctx.reply(
        f"**major-guild breakdown** — {len(rows)} cached, {len(major)} major\n"
        f"```\n{body}\n```\n{summary}\n"
        f"`~script major_breakdown <signal>` lists a signal's sole-cause guilds."
    )


async def _list_sole_cause(ctx, major, names, wanted: str) -> None:
    if wanted not in names:
        await ctx.reply(f"unknown signal `{wanted}` — try one of: {', '.join(names)}")
        return
    tags = sorted(tag for tag, met in major if met == {wanted})
    if not tags:
        await ctx.reply(f"no guild is major on `{wanted}` alone.")
        return
    shown = tags[:_MAX_LISTED]
    suffix = "" if len(shown) == len(tags) else f"\n…and {len(tags) - len(shown)} more"
    await ctx.reply(
        f"**{len(tags)}** guilds are major on `{wanted}` alone:\n"
        f"```\n{' '.join(shown)}\n```{suffix}"
    )
