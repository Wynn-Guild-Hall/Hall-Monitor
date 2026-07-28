"""``~script major_table`` — every cached guild, one row, signals as columns.

Sent as a CSV attachment: ~280 rows is well past what a Discord message
holds, and a spreadsheet is what you actually want for deciding which
threshold to move.

``true`` / ``false`` / empty, where empty means the signal was never
evaluated. Pass ``major`` to restrict it to guilds that qualified.
"""

import csv
import io

import discord

from . import _signal_rows


def _value(raw) -> str:
    if raw is True:
        return "true"
    return "false" if raw is False else ""


async def main(ctx, *args: str) -> None:
    rows = await _signal_rows.load()
    if not rows:
        await ctx.reply(
            "no major-guild cache rows yet — run `~script refresh_major` first."
        )
        return

    only_major = bool(args) and args[0].lower() in {"major", "yes", "true"}
    if only_major:
        rows = [row for row in rows if row.major]

    columns = _signal_rows.columns(rows)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["tag", "major", *columns])
    for row in rows:
        writer.writerow(
            [row.tag, "true" if row.major else "false"]
            + [_value(row.signals.get(name)) for name in columns]
        )

    payload = io.BytesIO(buffer.getvalue().encode("utf-8"))
    name = "major-guilds.csv" if only_major else "all-guilds.csv"
    major = sum(1 for row in rows if row.major)
    unevaluated = sum(1 for row in rows if row.skipped)

    summary = f"{len(rows)} guilds, {major} major"
    if unevaluated:
        summary += f" · ⚠ {unevaluated} have unevaluated signals (blank cells)"
    await ctx.reply(summary, file=discord.File(payload, filename=name))
