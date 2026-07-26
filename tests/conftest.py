"""Shared pytest fixtures for the Hall-Monitor test suite."""

import pytest
from tortoise import Tortoise


@pytest.fixture
async def db():
    """In-memory SQLite bound to the same models the app uses."""
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["hall_monitor.db.models"]},
    )
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()
