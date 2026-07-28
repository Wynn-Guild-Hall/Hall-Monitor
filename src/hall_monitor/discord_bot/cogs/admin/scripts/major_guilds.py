"""``~script major_guilds`` — the guilds that qualified, and on what.

Short enough to read in the channel, unlike the full table. Each row
shows which signals carried the guild, so a run of guilds riding on one
column is visible at a glance.
"""

from . import _signal_rows


async def main(ctx, *args: str) -> None:
    rows = [row for row in await _signal_rows.load() if row.major]
    if not rows:
        await ctx.reply(
            "no major guilds cached — run `~script refresh_major` first."
        )
        return

    columns = _signal_rows.columns(rows)
    heads = [_signal_rows.ABBREVIATIONS.get(name, name[:6]) for name in columns]
    tag_width = max(len(row.tag) for row in rows + [_signal_rows.Row("guild", False, {})])

    lines = [
        "guild".ljust(tag_width)
        + "  "
        + " ".join(head.rjust(6) for head in heads)
    ]
    # Sole-cause guilds first: they're the ones a threshold change moves.
    for row in sorted(rows, key=lambda r: (len(r.met), r.tag)):
        lines.append(
            row.tag.ljust(tag_width)
            + "  "
            + " ".join(
                _signal_rows.cell(row.signals.get(name)).rjust(6) for name in columns
            )
        )

    sole = sum(1 for row in rows if len(row.met) == 1)
    header = (
        f"**{len(rows)} major guilds** — {sole} qualify on a single signal, "
        f"sorted fewest-signals first.\n"
    )
    blocks = _signal_rows.chunk(lines, prefix=header)
    await ctx.reply(header + blocks[0])
    for block in blocks[1:]:
        await ctx.send(block)
