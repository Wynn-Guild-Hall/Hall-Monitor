"""``~script refresh_notability`` — run the notability sweep on demand.

The sweep otherwise only runs on the hourly scheduler, and it takes
minutes, so this reports progress into the invoking message rather than
going quiet and hoping.

Triggering it here rather than from a ``docker exec`` matters: the bot
process is the only intended writer to the SQLite file, and a second
process running the same sweep contends for the write lock hard enough to
drop guilds with "database is locked".
"""

import time

from hall_monitor.services import notability

# Discord allows roughly 5 edits per 5 s on a channel. A sweep steps
# through a hundred-odd guilds, so throttle well clear of that.
_EDIT_INTERVAL_S = 5.0


async def main(ctx, *args: str) -> None:
    # `full` evaluates every signal instead of stopping at the first hit —
    # a per-guild request for every candidate, so it's much slower. Only
    # worth it when you're tuning thresholds rather than deciding
    # notability.
    exhaustive = bool(args) and args[0].lower() in {"full", "exhaustive"}

    if notability.is_refreshing():
        await ctx.reply(
            "a notability refresh is already running — this one would have "
            "produced the same numbers, so it wasn't started."
        )
        return

    mode = " (full)" if exhaustive else ""
    message = await ctx.reply(f"notability refresh{mode}: loading leaderboards…")
    last_edit = time.monotonic()

    async def on_progress(done: int, total: int) -> None:
        nonlocal last_edit
        now = time.monotonic()
        if now - last_edit < _EDIT_INTERVAL_S and done != total:
            return
        last_edit = now
        await message.edit(content=f"notability refresh: {done}/{total} guilds…")

    summary = await notability.refresh_all(
        on_progress=on_progress, exhaustive=exhaustive
    )
    if summary is None:
        # Lost a race with the scheduler between the check and the call.
        await message.edit(content="a notability refresh was already running.")
        return

    lines = [
        f"notability refresh{mode} done in {summary.seconds:.0f}s",
        f"- {summary.evaluated} guilds evaluated, {summary.notable} notable",
    ]
    if summary.failed:
        lines.append(
            f"- {summary.failed} failed (kept their previous value) — see logs"
        )
    await message.edit(content="\n".join(lines))
