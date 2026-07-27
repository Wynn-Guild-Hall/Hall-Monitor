"""Tortoise ORM configuration consumed by both the app and Aerich CLI."""

import logging

from tortoise import Tortoise

from hall_monitor.config import settings

logger = logging.getLogger(__name__)

TORTOISE_ORM = {
    "connections": {"default": settings.sqlite_url},
    "apps": {
        "models": {
            "models": ["hall_monitor.db.models", "aerich.models"],
            "default_connection": "default",
        }
    },
}

# Columns added to models that already had a table in production.
#
# `generate_schemas(safe=True)` creates missing *tables* and stops there:
# a new column on an existing model is silently absent until something
# SELECTs it and SQLite raises `no such column`, which for us means every
# verify failing the moment a new image starts. Aerich owns real
# migrations, but it was never initialised against the deployed database
# (`aerich init` ran, `init-db` never did), so nothing else closes this.
#
# Additive only. Anything that drops or retypes a column needs a real
# migration and a maintenance window.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("delegate", "mc_username", "VARCHAR(16)"),
    ("pending_invite", "mc_username", "VARCHAR(16)"),
)


async def ensure_columns() -> None:
    """Add any column in :data:`_ADDED_COLUMNS` the live schema is missing."""
    connection = Tortoise.get_connection("default")
    for table, column, ddl in _ADDED_COLUMNS:
        _, rows = await connection.execute_query(f"PRAGMA table_info({table})")
        present = {row["name"] for row in rows}
        if not present:
            continue  # table isn't there yet; generate_schemas will build it whole
        if column in present:
            continue
        await connection.execute_script(
            f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
        )
        logger.info("schema: added %s.%s", table, column)
