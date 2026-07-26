"""Sidecar route coverage — /health, /verify, /join/lookup.

Uses :class:`httpx.AsyncClient` with :class:`ASGITransport` so requests
run on the same event loop as the Tortoise fixture; the default
``fastapi.testclient.TestClient`` spawns a portal loop and Tortoise
reconnects on the fresh loop, losing our in-memory schema.

The sidecar factory needs a ``bot`` object but only reads
``bot.get_guild(id)`` for the contacts flow, so a plain object with that
method suffices — we don't spin up a real discord.py Client.
"""

import httpx
import pytest
from fastapi import FastAPI

from hall_monitor.db.models import (
    Delegate,
    ForceOverride,
    GuildContact,
    NotabilityCache,
)
from hall_monitor.sidecar import build_app


class _StubBot:
    def get_guild(self, _id):
        return None


@pytest.fixture
def non_mocked_hosts() -> list[str]:
    """Let ASGI-transport calls to the test server bypass pytest-httpx."""
    return ["testserver"]


@pytest.fixture
def app() -> FastAPI:
    return build_app(_StubBot())


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_health_returns_200(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_verify_returns_kick_message_shape(client):
    r = await client.get("/api/verify/some-uuid/hall%20request%205")
    assert r.status_code == 200
    assert "kick_message" in r.json()


async def test_join_lookup_unknown_username_returns_404(client, httpx_mock):
    httpx_mock.add_response(
        url="https://api.mojang.com/users/profiles/minecraft/nobody",
        status_code=404,
    )
    r = await client.get("/api/join/lookup?username=nobody")
    assert r.status_code == 404


async def test_join_lookup_not_chief_or_owner(client, httpx_mock):
    httpx_mock.add_response(
        url="https://api.mojang.com/users/profiles/minecraft/recruit_user",
        json={"id": "u1", "name": "recruit_user"},
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/player/u1",
        json={"guild": {"name": "n", "prefix": "OTHR", "rank": "RECRUIT"}},
    )
    r = await client.get("/api/join/lookup?username=recruit_user")
    body = r.json()
    assert body["eligible"] is False
    assert body["reason"] == "not chief or owner"
    assert body["guild_tag"] == "OTHR"
    assert body["mc_username"] == "recruit_user"


async def test_join_lookup_no_guild(client, httpx_mock):
    httpx_mock.add_response(
        url="https://api.mojang.com/users/profiles/minecraft/guildless",
        json={"id": "u1", "name": "guildless"},
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/player/u1",
        json={"guild": None},
    )
    r = await client.get("/api/join/lookup?username=guildless")
    body = r.json()
    assert body["eligible"] is False
    assert body["reason"] == "not chief or owner"
    assert body["guild_tag"] is None


async def test_join_lookup_guild_not_notable(db, client, httpx_mock):
    httpx_mock.add_response(
        url="https://api.mojang.com/users/profiles/minecraft/chief",
        json={"id": "u1", "name": "chief"},
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/player/u1",
        json={"guild": {"name": "Small Guild", "prefix": "SMLL", "rank": "CHIEF"}},
    )
    # Cache SMLL as not notable so is_notable takes the fast path.
    await NotabilityCache.create(
        guild_tag="SMLL", is_notable=False, signals_json="{}"
    )
    r = await client.get("/api/join/lookup?username=chief")
    body = r.json()
    assert body["eligible"] is False
    assert body["reason"] == "guild not notable"
    assert body["guild_tag"] == "SMLL"


async def test_join_lookup_happy_path(db, client, httpx_mock):
    httpx_mock.add_response(
        url="https://api.mojang.com/users/profiles/minecraft/wenweia",
        json={"id": "chief-uuid", "name": "wenweia"},
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/player/chief-uuid",
        json={"guild": {"name": "Wynncraft Veterans", "prefix": "VETS", "rank": "OWNER"}},
    )
    await NotabilityCache.create(
        guild_tag="VETS", is_notable=True, signals_json="{}"
    )
    r = await client.get("/api/join/lookup?username=wenweia")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "eligible": True,
        "mc_username": "wenweia",
        "guild_tag": "VETS",
        "current_contacts_per_role": {
            "events": None,
            "housing": None,
            "warring": None,
            "ownership": None,
        },
    }


async def test_join_lookup_populates_contacts_when_assigned(db, client, httpx_mock):
    """Once a Delegate + GuildContact row exists, the API surfaces the holder."""
    httpx_mock.add_response(
        url="https://api.mojang.com/users/profiles/minecraft/wenweia",
        json={"id": "chief-uuid", "name": "wenweia"},
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/player/chief-uuid",
        json={"guild": {"name": "Wynncraft Veterans", "prefix": "VETS", "rank": "OWNER"}},
    )
    await NotabilityCache.create(guild_tag="VETS", is_notable=True, signals_json="{}")
    holder = await Delegate.create(
        mc_uuid="holder-uuid", discord_user_id=42, guild_tag="VETS"
    )
    await GuildContact.create(guild_tag="VETS", role="warring", delegate=holder)

    r = await client.get("/api/join/lookup?username=wenweia")
    body = r.json()
    assert body["current_contacts_per_role"]["warring"] == "holder-uuid"
    assert body["current_contacts_per_role"]["housing"] is None


async def test_join_lookup_force_override_makes_notable(db, client, httpx_mock):
    """A force-notable override alone is enough to pass the notability gate,
    even when there's no NotabilityCache row and every real signal would fail."""
    httpx_mock.add_response(
        url="https://api.mojang.com/users/profiles/minecraft/chief",
        json={"id": "u1", "name": "chief"},
    )
    httpx_mock.add_response(
        url="https://api.wynncraft.com/v3/player/u1",
        json={"guild": {"name": "Forced Guild", "prefix": "FRCD", "rank": "CHIEF"}},
    )
    await ForceOverride.create(kind="notable", subject="FRCD", expires_at=None)
    r = await client.get("/api/join/lookup?username=chief")
    body = r.json()
    assert body["eligible"] is True
    assert body["guild_tag"] == "FRCD"
