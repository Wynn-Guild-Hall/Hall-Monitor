"""Startup migration: fresh installs, the pre-migration production
database, and the guards that stop a bad baseline from being recorded.

These run against real SQLite files rather than `:memory:` — Aerich opens
its own connection from `TORTOISE_ORM`, and two connections to `:memory:`
are two different databases.
"""

import re

import pytest
from aerich.models import Aerich
from tortoise import Tortoise

from hall_monitor import db as db_module
from hall_monitor.db.models import Delegate, MajorGuildCache


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


@pytest.fixture
def only_the_initial_migration(tmp_path, monkeypatch):
    """Point Aerich at a copy of the migrations holding just the first.

    Baselining is only ever correct against a database that predates
    *every* migration, and the production one was baselined in Stage 7 — so
    with the real directory the guard below fires instead, which is the
    point of the guard and the reason these cases supply their own.
    """
    versions = tmp_path / "migrations" / "models"
    versions.mkdir(parents=True)
    source = sorted((db_module.MIGRATIONS_DIR / "models").glob("[0-9]*.py"))[0]
    (versions / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", tmp_path / "migrations")
    return versions


async def test_pre_migration_database_is_baselined_not_rebuilt(
    connected, only_the_initial_migration, caplog
):
    """Running the initial migration for real would `CREATE TABLE` over
    live tables. It gets recorded as applied instead."""
    await _make_pre_migration_database()
    await Delegate.create(mc_uuid="keep-me", discord_user_id=7, guild_tag="VETS")

    await db_module.migrate()

    assert await Aerich.filter(app="models").exists()
    assert await Delegate.filter(mc_uuid="keep-me").exists(), "data survived"
    assert "baselined" in caplog.text


async def test_baselined_database_then_migrates_normally(
    connected, only_the_initial_migration
):
    """After the baseline, it's an ordinary managed database."""
    await _make_pre_migration_database()
    await db_module.migrate()
    before = await Aerich.filter(app="models").count()

    await db_module.migrate()

    assert await Aerich.filter(app="models").count() == before


async def test_baseline_refuses_when_the_live_schema_has_drifted(
    connected, only_the_initial_migration
):
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


# --------------------------------------------------------------------------
# Upgrading a database that has data in it
# --------------------------------------------------------------------------


_ADD_COLUMN = re.compile(
    r'ADD\s+"(?P<column>[^"]+)"\s+(?P<rest>[^;]*)', re.IGNORECASE
)


def test_no_migration_adds_a_not_null_column_without_a_default():
    """SQLite refuses `ADD ... NOT NULL` on a table that already has rows
    unless there's a value to give them. An empty test database accepts
    it happily, so this is invisible until a deploy — which is exactly
    how `metrics_json` took the bot down. Aerich generates the offending
    form on its own, so the check has to live here rather than in review.
    """
    offenders = []
    for path in sorted((db_module.MIGRATIONS_DIR / "models").glob("[0-9]*.py")):
        source = path.read_text(encoding="utf-8")
        # Only the ALTER path matters; a NOT NULL column inside a CREATE
        # TABLE is fine, since the table starts empty by definition.
        for line in source.splitlines():
            if "ALTER TABLE" not in line.upper():
                continue
            match = _ADD_COLUMN.search(line)
            if match is None:
                continue
            rest = match.group("rest").upper()
            if "NOT NULL" in rest and "DEFAULT" not in rest:
                offenders.append(f"{path.name}: {match.group('column')}")
    assert not offenders, (
        "these would fail on any database with rows in the table: "
        + ", ".join(offenders)
    )


_DROP_TABLE = re.compile(r'DROP\s+TABLE\s+(IF\s+EXISTS\s+)?"(?P<table>[^"]+)"', re.I)


def test_no_migration_drops_a_table_another_one_created():
    """Aerich renders a *rename* as CREATE-then-DROP, which is a data-losing
    no-op wearing a rename's clothes: every row goes, the next sweep
    rebuilds what it can, and nothing looks broken until somebody asks
    why the roster was empty for an hour. It generated exactly that for
    `notability_cache` → `major_guild_cache`.

    So: no upgrade may drop a table an earlier upgrade created. A table
    genuinely being retired would need this test updated, which is the
    point — it should be a decision, not a diff nobody read.
    """
    created, offenders = set(), []
    for path in sorted((db_module.MIGRATIONS_DIR / "models").glob("[0-9]*.py")):
        source = path.read_text(encoding="utf-8")
        upgrade = source.split("async def downgrade")[0]
        for match in re.finditer(r'CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?"([^"]+)"', upgrade, re.I):
            created.add(match.group(2))
        for match in _DROP_TABLE.finditer(upgrade):
            if match.group("table") in created:
                offenders.append(f"{path.name}: {match.group('table')}")
    assert not offenders, (
        "these drop a table an earlier migration created — if it's a "
        "rename, use ALTER TABLE ... RENAME TO: " + ", ".join(offenders)
    )


async def test_renaming_a_table_keeps_its_rows(connected):
    """The production condition for migration 11. A fresh-schema test
    can't model it: the rename is invisible on an empty table, which is
    exactly why the generated version looked fine."""
    await db_module.migrate()

    await MajorGuildCache.create(
        guild_tag="VETS", is_major=True, signals_json="{}", guild_name="Returners"
    )
    await db_module.migrate()  # a second pass must not re-run the rename

    cached = await MajorGuildCache.get(guild_tag="VETS")
    assert cached.guild_name == "Returners"
    assert cached.is_major is True


async def test_migrations_apply_to_a_database_with_rows(connected):
    """The production condition, and the one a fresh-schema test can't
    model. Every column added after the initial migration lands on a
    table that already has data."""
    await db_module.migrate()

    await Delegate.create(mc_uuid="u", discord_user_id=1, guild_tag="VETS")
    await MajorGuildCache.create(
        guild_tag="VETS", is_major=True, signals_json="{}"
    )

    # Re-running is a no-op, but the rows above are what make a second
    # pass over the same schema meaningful: they'd block any ALTER that
    # needs a default and hasn't got one.
    await db_module.migrate()

    cached = await MajorGuildCache.get(guild_tag="VETS")
    assert cached.metrics_json == "{}", "existing rows got the default"
