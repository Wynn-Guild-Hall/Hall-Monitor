"""`ensure_columns` — the additive-column step `generate_schemas(safe=True)`
doesn't do, and the deployed database has no Aerich history to do for it."""

from tortoise import Tortoise

from hall_monitor.db import ensure_columns
from hall_monitor.db.models import Delegate


async def _columns(table: str) -> set[str]:
    connection = Tortoise.get_connection("default")
    _, rows = await connection.execute_query(f"PRAGMA table_info({table})")
    return {row["name"] for row in rows}


async def test_adds_a_column_missing_from_a_live_table(db):
    """The failure this prevents: a new column on an existing table is
    absent until something SELECTs it, and then every verify 500s."""
    connection = Tortoise.get_connection("default")
    await connection.execute_script("ALTER TABLE delegate DROP COLUMN mc_username")
    assert "mc_username" not in await _columns("delegate")

    await ensure_columns()

    assert "mc_username" in await _columns("delegate")
    # And the ORM can round-trip through it.
    await Delegate.create(
        mc_uuid="u", mc_username="Holidaze", discord_user_id=1, guild_tag="VETS"
    )
    assert (await Delegate.get(mc_uuid="u")).mc_username == "Holidaze"


async def test_is_idempotent(db):
    """Runs on every boot; a second pass must not error on an existing column."""
    await ensure_columns()
    await ensure_columns()
    assert "mc_username" in await _columns("delegate")


async def test_leaves_a_table_that_doesnt_exist_yet_alone(db, monkeypatch):
    """A fresh database gets its tables whole from generate_schemas — this
    step must not try to ALTER one into being."""
    monkeypatch.setattr(
        "hall_monitor.db._ADDED_COLUMNS",
        (("not_a_table", "some_column", "VARCHAR(8)"),),
    )
    await ensure_columns()  # must not raise
