"""FastAPI sidecar factory.

The sidecar runs in-process with the discord.py bot so routes can call bot
methods and share the Tortoise ORM connection.
"""

from discord.ext import commands
from fastapi import FastAPI

from hall_monitor.sidecar.routes import health, join, verify


def build_app(bot: commands.Bot) -> FastAPI:
    app = FastAPI(title="hall-monitor")
    app.state.bot = bot
    app.include_router(health.router)
    app.include_router(verify.router)
    app.include_router(join.router)
    return app
