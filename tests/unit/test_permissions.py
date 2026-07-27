"""Role gates: the tiers nest upward, and nobody's IDs are frozen at import."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from hall_monitor.discord_bot import permissions

ROLE_IDS = {
    "delegate_role_id": 10,
    "events_contact_role_id": 20,
    "housing_contact_role_id": 21,
    "warring_contact_role_id": 22,
    "ownership_contact_role_id": 23,
    "janitor_role_id": 30,
    "monitor_role_id": 40,
}


@pytest.fixture(autouse=True)
def configured_roles(monkeypatch):
    for name, role_id in ROLE_IDS.items():
        monkeypatch.setattr(f"hall_monitor.discord_bot.permissions.settings.{name}", role_id)


def _ctx(*role_ids: int):
    ctx = MagicMock()
    ctx.guild = MagicMock()
    ctx.author.roles = [MagicMock(id=r) for r in role_ids]
    ctx.bot.can_run = AsyncMock(return_value=True)
    return ctx


async def _passes(gate, *role_ids: int) -> bool:
    """Run a gate's predicate directly — `commands.check` stashes it on the
    decorated callback, so borrow a throwaway one."""

    async def callback(ctx):
        ...

    gate()(callback)
    return await callback.__commands_checks__[0](_ctx(*role_ids))


NOBODY = ()
DELEGATE = (ROLE_IDS["delegate_role_id"],)
CONTACT = (ROLE_IDS["delegate_role_id"], ROLE_IDS["housing_contact_role_id"])
OWNERSHIP = (ROLE_IDS["delegate_role_id"], ROLE_IDS["ownership_contact_role_id"])
JANITOR = (ROLE_IDS["janitor_role_id"],)
MONITOR = (ROLE_IDS["monitor_role_id"],)


@pytest.mark.parametrize(
    "gate,roles,expected",
    [
        # A monitor runs everything. This is the case that shipped broken:
        # `~dash` told a monitor they hadn't got the role for it.
        (permissions.is_delegate, MONITOR, True),
        (permissions.is_contact, MONITOR, True),
        (permissions.is_ownership_contact, MONITOR, True),
        (permissions.is_janitor, MONITOR, True),
        (permissions.is_monitor, MONITOR, True),
        # A janitor runs everything below monitor.
        (permissions.is_delegate, JANITOR, True),
        (permissions.is_contact, JANITOR, True),
        (permissions.is_ownership_contact, JANITOR, True),
        (permissions.is_janitor, JANITOR, True),
        (permissions.is_monitor, JANITOR, False),
        # …and the nesting doesn't run the other way.
        (permissions.is_contact, CONTACT, True),
        (permissions.is_ownership_contact, CONTACT, False),
        (permissions.is_ownership_contact, OWNERSHIP, True),
        (permissions.is_janitor, OWNERSHIP, False),
        (permissions.is_delegate, DELEGATE, True),
        (permissions.is_contact, DELEGATE, False),
        (permissions.is_delegate, NOBODY, False),
        (permissions.is_janitor, NOBODY, False),
    ],
)
async def test_tier_nesting(gate, roles, expected):
    assert await _passes(gate, *roles) is expected


async def test_a_contact_counts_as_a_delegate():
    """Contacts hold the delegate role in practice, but the gate shouldn't
    depend on that having been applied correctly."""
    assert await _passes(
        permissions.is_contact, ROLE_IDS["events_contact_role_id"]
    ) is True
    assert await _passes(
        permissions.is_delegate, ROLE_IDS["events_contact_role_id"]
    ) is True


async def test_unset_role_ids_never_match(monkeypatch):
    """An unconfigured gate must fail closed, not admit everyone."""
    for name in ROLE_IDS:
        monkeypatch.setattr(f"hall_monitor.discord_bot.permissions.settings.{name}", 0)
    assert await _passes(permissions.is_monitor, 0) is False
    assert await _passes(permissions.is_janitor, 30) is False


async def test_gates_read_settings_at_call_time(monkeypatch):
    """IDs are deploy-time config; freezing them at import is what makes a
    settings change look like it did nothing."""
    monkeypatch.setattr(
        "hall_monitor.discord_bot.permissions.settings.monitor_role_id", 999
    )
    assert await _passes(permissions.is_monitor, 999) is True
    assert await _passes(permissions.is_monitor, ROLE_IDS["monitor_role_id"]) is False


def test_dm_context_is_rejected():
    ctx = _ctx()
    ctx.guild = None
    assert permissions.has_any_role(ctx, 10) is False
