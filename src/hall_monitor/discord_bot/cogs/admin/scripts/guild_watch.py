"""``~script guild_watch`` — ask Wynncraft where every delegate is now.

The same poll the hourly job runs: one request per live delegate, writing
each one's current guild. It changes no roles by itself — run
`~script reconcile` afterwards to act on what it found.

Split from the reconcile deliberately: this one costs a request per
delegate and answers "what's true in Wynncraft", while the reconcile is
free and answers "does Discord match". Pairing them by hand is how you
test a guild change without waiting for the hour.
"""

from hall_monitor.services import delegate_registry


async def main(ctx, *args: str) -> None:
    message = await ctx.reply("guild watch: asking Wynncraft about each delegate…")
    checked, external = await delegate_registry.refresh_current_guilds()
    await message.edit(
        content=(
            f"guild watch done — {checked} delegate(s) checked, {external} "
            "representing a guild they've left. "
            "Run `~script reconcile` to apply it."
        )
    )
