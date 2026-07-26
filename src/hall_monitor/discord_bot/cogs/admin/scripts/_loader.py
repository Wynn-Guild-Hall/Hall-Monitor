"""Discovery + dispatch for ad-hoc scripts under this directory.

Any ``foo.py`` here becomes ``~script foo`` provided it exposes
``async def main(ctx, *args)``. Files starting with ``_`` are skipped.
"""

import importlib
import pkgutil
from typing import Iterable


def discover_scripts() -> Iterable[str]:
    package = importlib.import_module(__package__)
    for _finder, name, is_pkg in pkgutil.iter_modules(package.__path__):
        if is_pkg or name.startswith("_"):
            continue
        yield name


async def run_script(name: str, ctx, *args: str) -> None:
    if name not in set(discover_scripts()):
        await ctx.send(f"unknown script: {name}")
        return
    module = importlib.import_module(f"{__package__}.{name}")
    main = getattr(module, "main", None)
    if main is None:
        await ctx.send(f"script {name!r} has no main(ctx, *args)")
        return
    await main(ctx, *args)
