"""Tortoise ORM configuration, and the startup migration step.

Schema changes are Aerich migrations, generated with
``aerich migrate --name <slug>`` and committed alongside the model edit.
:func:`migrate` applies them at boot — there is one process and one SQLite
file, so there's no window where two instances race to migrate, and a
deploy that needed a remembered manual step would eventually get one that
nobody remembered.

The awkward case this has to handle is the database that predates all of
this. Hall-Monitor ran for six stages on ``generate_schemas(safe=True)``,
which creates missing *tables* and stops — a column added to an existing
model stayed absent until something SELECTed it. Aerich was half set up
(``aerich init`` had run, ``init-db`` never had), so the production
database has every table, an empty ``aerich`` bookkeeping table, and no
record of a migration ever being applied. :func:`_baseline` recognises
that shape and records the initial migration as applied *without running
it*, since the tables it would create are already there.
"""

import logging
from pathlib import Path

from aerich import Command
from aerich.models import Aerich
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

_APP = "models"
_AERICH_TABLE = "aerich"

# Ships inside the package (see `[tool.setuptools.package-data]`), so this
# resolves the same from a source checkout and from site-packages.
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def migrate() -> None:
    """Bring the database up to the committed migrations.

    Three shapes, distinguished before anything is written:

    - **Fresh** — no tables. The initial migration creates them all,
      including Aerich's own bookkeeping table, so nothing needs to exist
      beforehand.
    - **Pre-migration** — tables present, nothing recorded as applied.
      Baselined; see :func:`_baseline`.
    - **Managed** — anything else. Apply what's outstanding.
    """
    command = Command(
        tortoise_config=TORTOISE_ORM, app=_APP, location=str(MIGRATIONS_DIR)
    )
    await command.init()

    if await _is_pre_migration_database():
        await _baseline(command)
        return

    applied = await command.upgrade()
    if applied:
        logger.info("db: applied migration(s) %s", ", ".join(applied))


async def _baseline(command: Command) -> None:
    """Record the initial migration as applied without running its SQL.

    Only ever correct when the live schema already matches that migration,
    so that is checked rather than assumed — a mismatch means some other
    drift is in play, and marking it migrated would bury the difference
    where the next real migration trips over it.
    """
    versions = sorted(p.name for p in _versions_dir().glob("[0-9]*.py"))
    if len(versions) != 1:
        raise RuntimeError(
            "refusing to baseline: this database predates migrations, but "
            f"{len(versions)} migrations exist ({', '.join(versions)}). "
            "Faking them all would skip real schema changes. Baseline by "
            "hand against the migration this database actually matches."
        )

    drift = await _schema_drift()
    if drift:
        raise RuntimeError(
            "refusing to baseline: the live schema doesn't match the models "
            f"({'; '.join(drift)}). Reconcile it first — see DESIGN.md §10."
        )

    faked = await command.upgrade(fake=True)
    logger.warning(
        "db: baselined a pre-migration database — recorded %s as applied "
        "without running it; the tables were already there",
        ", ".join(faked),
    )


async def _is_pre_migration_database() -> bool:
    """Tables built by the old path, with nothing recorded as applied.

    Note the bookkeeping table is *not* the signal: ``aerich.models`` is in
    the app's model list, so ``generate_schemas`` has been creating an
    empty ``aerich`` table since the scaffold. Its emptiness is what marks
    the database as unmanaged.
    """
    tables = await _table_names()
    if not tables & _model_tables():
        return False  # fresh install — the initial migration builds it all
    if _AERICH_TABLE not in tables:
        return True
    return not await Aerich.filter(app=_APP).exists()


async def _schema_drift() -> list[str]:
    """Per-table differences between the live columns and the models."""
    differences = []
    live_tables = await _table_names()
    for model in Tortoise.apps[_APP].values():
        table = model._meta.db_table
        if table == _AERICH_TABLE or table not in live_tables:
            continue
        wanted = set(model._meta.fields_db_projection.values())
        present = await _column_names(table)
        if missing := wanted - present:
            differences.append(f"{table} is missing {', '.join(sorted(missing))}")
    return differences


def _versions_dir() -> Path:
    return MIGRATIONS_DIR / _APP


def _model_tables() -> set[str]:
    return {
        model._meta.db_table for model in Tortoise.apps[_APP].values()
    } - {_AERICH_TABLE}


async def _table_names() -> set[str]:
    connection = Tortoise.get_connection("default")
    _, rows = await connection.execute_query(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    return {row["name"] for row in rows}


async def _column_names(table: str) -> set[str]:
    connection = Tortoise.get_connection("default")
    _, rows = await connection.execute_query(f"PRAGMA table_info({table})")
    return {row["name"] for row in rows}
