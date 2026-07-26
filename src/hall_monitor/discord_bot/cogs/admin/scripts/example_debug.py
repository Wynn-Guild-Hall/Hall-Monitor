"""Placeholder script demonstrating the ``async def main(ctx, *args)`` contract."""


async def main(ctx, *args: str) -> None:
    await ctx.send(f"hello from example_debug (args={args})")
