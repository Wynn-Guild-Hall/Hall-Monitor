"""Startup migration: fresh installs, the pre-migration production
database, and the guards that stop a bad baseline from being recorded.

These run against real SQLite files rather than `:memory:` — Aerich opens
its own connection from `TORTOISE_ORM`, and two connections to `:memory:`
are two different databases.
"""

import pytest
from aerich.models import Aerich
from tortoise import Tortoise

from hall_monitor import db as db_module
from hall_monitor.db.models import Delegate


@pytest.fixture
def sqlite_file(tmp_path, monkeypatch):
    """Point both the app config and Aerich at a scratch database file."""
    path = tmp_path / "hall-monitor.db"
    config = {
        "connections": {"default": f"sqlite://{path}"},
        "apps": {
            "models": {
                "models": ["hall_monitor.db.models", "aerich.models"],
                "default_connection": "default",
            }
        },
    }
    monkeypatch.setattr(db_module, "TORTOISE_ORM", config)
    return path


async def _connect(config):
    await Tortoise.init(config=config)


async def _tables() -> set[str]:
    connection = Tortoise.get_connection("default")
    _, rows = await connection.execute_query(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    return {row["name"] for row in rows}


@pytest.fixture
async def connected(sqlite_file):
    await _connect(db_module.TORTOISE_ORM)
    yield sqlite_file
    await Tortoise.close_connections()


# --------------------------------------------------------------------------
# Fresh install
# --------------------------------------------------------------------------


async def test_fresh_database_gets_the_whole_schema(connected):
    """No `generate_schemas` in the boot path any more — the initial
    migration has to build everything, including Aerich's own table."""
    await db_module.migrate()

    tables = await _tables()
    assert {"delegate", "pending_invite", "guild_contact", "aerich"} <= tables
    # And it's recorded, so the next boot is a no-op rather than a re-run.
    assert await Aerich.filter(app="models").exists()


async def test_fresh_database_supports_the_current_models(connected):
    """The columns added after the scaffold have to be in the baseline."""
    await db_module.migrate()

    await Delegate.create(
        mc_uuid="u", mc_username="Holidaze", discord_user_id=1, guild_tag="VETS"
    )
    assert (await Delegate.get(mc_uuid="u")).mc_username == "Holidaze"


async def test_migrate_is_idempotent(connected):
    await db_module.migrate()
    applied = await Aerich.filter(app="models").count()
    await db_module.migrate()
    assert await Aerich.filter(app="models").count() == applied


# --------------------------------------------------------------------------
# The pre-migration production database
# --------------------------------------------------------------------------


async def _make_pre_migration_database() -> None:
    """The shape six stages of `generate_schemas(safe=True)` left behind:
    every table present, an empty `aerich` table, nothing recorded."""
    await Tortoise.generate_schemas(safe=True)
    assert "aerich" in await _tables(), "aerich.models is in the app's model list"
    assert not await Aerich.filter(app="models").exists()


async def test_pre_migration_database_is_baselined_not_rebuilt(connected, caplog):
    """Running the initial migration for real would `CREATE TABLE` over
    live tables. It gets recorded as applied instead."""
    await _make_pre_migration_database()
    await Delegate.create(mc_uuid="keep-me", discord_user_id=7, guild_tag="VETS")

    await db_module.migrate()

    assert await Aerich.filter(app="models").exists()
    assert await Delegate.filter(mc_uuid="keep-me").exists(), "data survived"
    assert "baselined" in caplog.text


async def test_baselined_database_then_migrates_normally(connected):
    """After the baseline, it's an ordinary managed database."""
    await _make_pre_migration_database()
    await db_module.migrate()
    before = await Aerich.filter(app="models").count()

    await db_module.migrate()

    assert await Aerich.filter(app="models").count() == before


async def test_baseline_refuses_when_the_live_schema_has_drifted(connected):
    """Faking a baseline onto a schema that doesn't match the migration
    buries the difference until some later migration trips over it."""
    await _make_pre_migration_database()
    connection = Tortoise.get_connection("default")
    await connection.execute_script("ALTER TABLE delegate DROP COLUMN mc_username")

    with pytest.raises(RuntimeError, match="doesn't match the models"):
        await db_module.migrate()

    assert not await Aerich.filter(app="models").exists(), "nothing recorded"


async def test_baseline_refuses_when_more_than_one_migration_exists(
    connected, tmp_path, monkeypatch
):
    """A pre-migration database showing up after later migrations were
    written can't be faked wholesale — that would skip real changes."""
    versions = tmp_path / "migrations" / "models"
    versions.mkdir(parents=True)
    for name in ("0_20260101000000_init.py", "1_20260202000000_add_thing.py"):
        (versions / name).write_text("async def upgrade(db):\n    return ''\n")
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", tmp_path / "migrations")

    await _make_pre_migration_database()

    with pytest.raises(RuntimeError, match="refusing to baseline"):
        await db_module.migrate()


# --------------------------------------------------------------------------
# Shipping
# --------------------------------------------------------------------------


def test_the_initial_migration_is_committed():
    """`pip install .` copies these as package data. If the directory is
    empty the container boots with nothing to apply and no error."""
    versions = sorted(
        p.name for p in (db_module.MIGRATIONS_DIR / "models").glob("[0-9]*.py")
    )
    assert versions, "no migration files found next to the package"
    assert versions[0].startswith("0_")
