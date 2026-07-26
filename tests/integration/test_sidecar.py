"""Sidecar route coverage — /health, /verify, /join/lookup.

Uses :class:`httpx.AsyncClient` with :class:`ASGITransport` so requests
run on the same event loop as the Tortoise fixture; the default
``fastapi.testclient.TestClient`` spawns a portal loop and Tortoise
reconnects on the fresh loop, losing our in-memory schema.

The sidecar factory needs a ``bot`` object but only reads
``bot.get_guild(id)`` for the contacts flow, so a plain object with that
method suffices — we don't spin up a real discord.py Client.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from hall_monitor.db.models import (
    Delegate,
    ForceOverride,
    GuildContact,
    NotabilityCache,
    PendingInvite,
)
from hall_monitor.sidecar import build_app


class _StubBot:
    def get_guild(self, _id):
        return None


def _bot_with_welcome_channel(mint_code: str = "invite-code"):
    """A discord-like bot mock whose welcome channel mints a fake invite."""
    channel = MagicMock()
    invite = MagicMock()
    invite.code = mint_code
    invite.url = f"https://discord.gg/{mint_code}"
    channel.create_invite = AsyncMock(return_value=invite)

    guild = MagicMock()
    guild.get_channel = lambda _id: channel
    guild.get_member = lambda _id: None  # no live delegates by default

    bot = MagicMock()
    bot.get_guild = lambda _id: guild
    bot.http = MagicMock()
    bot.http.delete_invite = AsyncMock()
    return bot, channel


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


async def test_verify_mistyped_code_kick_message(client):
    r = await client.get("/api/verify/some-uuid/HALL1X")
    assert r.status_code == 200
    assert "isn't a Guild Hall code" in r.json()["kick_message"]


async def test_verify_ignores_ordinary_chat(client):
    """The route prefix is only `hall`, so conversation reaches us as well.
    Nobody gets disconnected for saying "hallo"."""
    r = await client.get("/api/verify/some-uuid/o%20everyone")
    assert r.json()["kick_message"] is None


async def test_verify_invalid_role_bits_kick_message(client):
    # Bit 4 is reserved / unknown → decode raises → error kick.
    r = await client.get("/api/verify/some-uuid/HALL16")
    assert "role we don't recognise" in r.json()["kick_message"]


async def test_verify_happy_path_mints_invite(db, app, httpx_mock, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.sidecar.routes.verify.settings.discord_guild_id", 1
    )
    monkeypatch.setattr(
        "hall_monitor.sidecar.routes.verify.settings.welcome_channel_id", 2
    )
    bot, channel = _bot_with_welcome_channel("abc123")
    app.state.bot = bot

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        httpx_mock.add_response(
            url="https://api.wynncraft.com/v3/player/uuid-chief",
            json={"guild": {"name": "Wynncraft Veterans", "prefix": "VETS", "rank": "CHIEF"}},
        )
        await NotabilityCache.create(guild_tag="VETS", is_notable=True, signals_json="{}")

        r = await c.get("/api/verify/uuid-chief/HALL05")
        kick = r.json()["kick_message"]
        assert "discord.gg/abc123" in kick
        assert "VETS" in kick

    # A PendingInvite row was persisted with the correct role bits.
    row = await PendingInvite.get(mc_uuid="uuid-chief")
    assert row.roles_bits == 5
    assert row.discord_invite_code == "abc123"
    channel.create_invite.assert_awaited_once()


async def test_verify_not_chief_or_owner_kick_message(db, app, httpx_mock, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.sidecar.routes.verify.settings.discord_guild_id", 1
    )
    monkeypatch.setattr(
        "hall_monitor.sidecar.routes.verify.settings.welcome_channel_id", 2
    )
    bot, _ = _bot_with_welcome_channel()
    app.state.bot = bot
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        httpx_mock.add_response(
            url="https://api.wynncraft.com/v3/player/uuid-recruit",
            json={"guild": {"name": "n", "prefix": "OTHR", "rank": "RECRUIT"}},
        )
        r = await c.get("/api/verify/uuid-recruit/HALL01")
        assert "chief or owner" in r.json()["kick_message"]


async def test_verify_guild_not_notable_kick_message(db, app, httpx_mock, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.sidecar.routes.verify.settings.discord_guild_id", 1
    )
    monkeypatch.setattr(
        "hall_monitor.sidecar.routes.verify.settings.welcome_channel_id", 2
    )
    bot, _ = _bot_with_welcome_channel()
    app.state.bot = bot
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        httpx_mock.add_response(
            url="https://api.wynncraft.com/v3/player/uuid-chief",
            json={"guild": {"name": "Small", "prefix": "SMLL", "rank": "CHIEF"}},
        )
        await NotabilityCache.create(
            guild_tag="SMLL", is_notable=False, signals_json="{}"
        )
        r = await c.get("/api/verify/uuid-chief/HALL01")
        assert "SMLL" in r.json()["kick_message"]
        assert "notable guild" in r.json()["kick_message"]


async def test_verify_already_a_delegate_kick_message(db, app, httpx_mock, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.sidecar.routes.verify.settings.discord_guild_id", 1
    )
    monkeypatch.setattr(
        "hall_monitor.sidecar.routes.verify.settings.welcome_channel_id", 2
    )
    bot, channel = _bot_with_welcome_channel()
    # Say the delegate's Discord user IS in the guild.
    guild = bot.get_guild(1)
    guild.get_member = lambda uid: MagicMock() if uid == 42 else None
    app.state.bot = bot

    await NotabilityCache.create(guild_tag="VETS", is_notable=True, signals_json="{}")
    await Delegate.create(
        mc_uuid="uuid-existing", discord_user_id=42, guild_tag="VETS"
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        httpx_mock.add_response(
            url="https://api.wynncraft.com/v3/player/uuid-existing",
            json={"guild": {"name": "Wynncraft Veterans", "prefix": "VETS", "rank": "CHIEF"}},
        )
        r = await c.get("/api/verify/uuid-existing/HALL01")
        assert "already verified" in r.json()["kick_message"]
    # No fresh invite was minted.
    channel.create_invite.assert_not_awaited()


async def test_verify_second_request_revokes_first_invite(db, app, httpx_mock, monkeypatch):
    """Second /api/verify for the same UUID replaces the invite in place."""
    monkeypatch.setattr(
        "hall_monitor.sidecar.routes.verify.settings.discord_guild_id", 1
    )
    monkeypatch.setattr(
        "hall_monitor.sidecar.routes.verify.settings.welcome_channel_id", 2
    )
    bot, channel = _bot_with_welcome_channel("first")
    app.state.bot = bot
    await NotabilityCache.create(guild_tag="VETS", is_notable=True, signals_json="{}")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        httpx_mock.add_response(
            url="https://api.wynncraft.com/v3/player/uuid-chief",
            json={"guild": {"name": "Wynncraft Veterans", "prefix": "VETS", "rank": "CHIEF"}},
            is_reusable=True,
        )
        r1 = await c.get("/api/verify/uuid-chief/HALL01")
        assert "discord.gg/first" in r1.json()["kick_message"]

        # Swap the next mint to a fresh code.
        second_invite = MagicMock()
        second_invite.code = "second"
        second_invite.url = "https://discord.gg/second"
        channel.create_invite = AsyncMock(return_value=second_invite)

        r2 = await c.get("/api/verify/uuid-chief/HALL01")
        assert "discord.gg/second" in r2.json()["kick_message"]

    # Old code was revoked; only the new row remains.
    bot.http.delete_invite.assert_awaited()
    rows = await PendingInvite.filter(mc_uuid="uuid-chief").all()
    assert len(rows) == 1
    assert rows[0].discord_invite_code == "second"


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
