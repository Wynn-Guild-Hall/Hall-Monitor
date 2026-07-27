"""Process entry point.

Runs the discord.py bot, FastAPI sidecar, and APScheduler in one asyncio
event loop so they share the same Tortoise ORM connection and the sidecar
routes can call bot methods directly.
"""

import asyncio
import logging

import uvicorn
from tortoise import Tortoise

from hall_monitor.config import settings
from hall_monitor.db import TORTOISE_ORM, ensure_columns
from hall_monitor.discord_bot import build_bot
from hall_monitor.scheduler import build_scheduler
from hall_monitor.sidecar import build_app

log = logging.getLogger("hall_monitor")


async def _run() -> None:
    await Tortoise.init(config=TORTOISE_ORM)
    await Tortoise.generate_schemas(safe=True)
    await ensure_columns()  # safe generation won't add columns to a live table

    bot = build_bot()
    app = build_app(bot)
    scheduler = build_scheduler(bot)

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.hall_monitor_port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    scheduler.start()
    try:
        await asyncio.gather(bot.start(settings.token), server.serve())
    finally:
        scheduler.shutdown(wait=False)
        await bot.close()
        await Tortoise.close_connections()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
