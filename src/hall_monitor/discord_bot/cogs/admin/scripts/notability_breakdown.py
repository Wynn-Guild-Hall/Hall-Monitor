"""``~script notability_breakdown`` — which criteria are producing notable guilds.

Reads the cache rather than re-evaluating, so it's instant and costs no
API calls. Run ``~script refresh_notability`` first if the numbers look
stale.

The column worth reading is **only**: guilds that signal alone made
notable. A signal with a high *any* but a low *only* is corroborating
other signals; a signal with a high *only* is the one setting the bar,
and the one to tighten.

``~script notability_breakdown <signal>`` lists the guilds a given signal
is solely responsible for.
"""

import json

from hall_monitor.db.models import NotabilityCache

# Canonical order; any signal added later still shows up, appended.
_KNOWN_SIGNALS = (
    "top25_average_online",
    "level_100_plus",
    "season_placement",
    "territory_ownership",
    "war_count",
    "force_override",
)

_MAX_LISTED = 60


def _true_signals(row) -> set[str]:
    """Signal names the row records as met. ``null`` means "not evaluated",
    which is not the same as met."""
    try:
        signals = json.loads(row.signals_json)
    except (TypeError, ValueError):
        return set()
    if not isinstance(signals, dict):
        return set()
    return {name for name, value in signals.items() if value is True}


def _ordered(names) -> list[str]:
    extra = sorted(name for name in names if name not in _KNOWN_SIGNALS)
    return [name for name in _KNOWN_SIGNALS if name in names] + extra


async def main(ctx, *args: str) -> None:
    rows = await NotabilityCache.all()
    if not rows:
        await ctx.reply(
            "no notability cache rows yet — run `~script refresh_notability` first."
        )
        return

    notable = [(row.guild_tag, _true_signals(row)) for row in rows if row.is_notable]
    seen: set[str] = set()
    for _tag, met in notable:
        seen |= met
    names = _ordered(seen | set(_KNOWN_SIGNALS))

    if args:
        await _list_sole_cause(ctx, notable, names, args[0])
        return

    counts = []
    for name in names:
        any_count = sum(1 for _tag, met in notable if name in met)
        only_count = sum(1 for _tag, met in notable if met == {name})
        counts.append((name, any_count, only_count))
    # Most-responsible first: that's the criterion to tighten.
    counts.sort(key=lambda row: (-row[2], -row[1], row[0]))

    width = max(len(name) for name, _a, _o in counts)
    lines = [f"{'signal'.ljust(width)}   any   only"]
    lines += [
        f"{name.ljust(width)} {any_count:>5} {only_count:>6}"
        for name, any_count, only_count in counts
    ]

    sole = sum(1 for _tag, met in notable if len(met) == 1)
    several = sum(1 for _tag, met in notable if len(met) > 1)
    unexplained = sum(1 for _tag, met in notable if not met)

    body = "\n".join(lines)
    summary = f"sole cause: {sole} · multiple: {several} · unexplained: {unexplained}"
    await ctx.reply(
        f"**notability breakdown** — {len(rows)} cached, {len(notable)} notable\n"
        f"```\n{body}\n```\n{summary}\n"
        f"`~script notability_breakdown <signal>` lists a signal's sole-cause guilds."
    )


async def _list_sole_cause(ctx, notable, names, wanted: str) -> None:
    if wanted not in names:
        await ctx.reply(f"unknown signal `{wanted}` — try one of: {', '.join(names)}")
        return
    tags = sorted(tag for tag, met in notable if met == {wanted})
    if not tags:
        await ctx.reply(f"no guild is notable on `{wanted}` alone.")
        return
    shown = tags[:_MAX_LISTED]
    suffix = "" if len(shown) == len(tags) else f"\n…and {len(tags) - len(shown)} more"
    await ctx.reply(
        f"**{len(tags)}** guilds are notable on `{wanted}` alone:\n"
        f"```\n{' '.join(shown)}\n```{suffix}"
    )
