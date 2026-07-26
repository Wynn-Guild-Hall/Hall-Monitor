"""Tortoise ORM configuration consumed by both the app and Aerich CLI."""

from hall_monitor.config import settings

TORTOISE_ORM = {
    "connections": {"default": settings.sqlite_url},
    "apps": {
        "models": {
            "models": ["hall_monitor.db.models", "aerich.models"],
            "default_connection": "default",
        }
    },
}
