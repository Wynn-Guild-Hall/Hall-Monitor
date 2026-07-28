"""`~force guild` — overriding where a member is treated as playing."""

from datetime import datetime, timedelta, timezone

from hall_monitor.db.models import Delegate, ForceOverride
from hall_monitor.services import delegate_registry


async def _delegate(user_id=1, *, tag="VETS", currently=None):
    return await Delegate.create(
        mc_uuid=f"uuid-{user_id}",
        discord_user_id=user_id,
        guild_tag=tag,
        current_guild_tag=currently,
    )


def _in(**kwargs) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**kwargs)


async def test_the_override_makes_someone_external(db):
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    assert await delegate_registry.is_external(delegate) is False

    await delegate_registry.set_forced_guild(1, "OTHR", _in(days=30))

    assert await delegate_registry.is_external(delegate) is True


async def test_the_override_can_undo_a_wrong_external(db):
    """The common case: a rep mid-transfer, or an alt showing the wrong
    guild. Forcing the guild they already represent puts them back."""
    delegate = await _delegate(1, tag="VETS", currently="OTHR")
    assert await delegate_registry.is_external(delegate) is True

    await delegate_registry.set_forced_guild(1, "VETS", _in(days=30))

    assert await delegate_registry.is_external(delegate) is False
    assert (
        await delegate_registry.standing(delegate, notable=True)
        == delegate_registry.DELEGATE
    )


async def test_the_override_beats_the_watch_not_the_other_way(db):
    """The watch rewrites `current_guild_tag` hourly, so an override stored
    in that column would survive exactly one sweep."""
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "OTHR", None)

    # A watch pass writing what Wynncraft says…
    delegate.current_guild_tag = "VETS"
    await delegate.save()

    # …doesn't touch the override.
    assert await delegate_registry.current_guild(delegate) == "OTHR"


async def test_an_expired_override_stops_counting(db):
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "OTHR", _in(days=-1))

    assert await delegate_registry.is_external(delegate) is False
    assert await ForceOverride.filter(kind="guild").count() == 1, (
        "left in place so a janitor can see what ran out"
    )


async def test_a_permanent_override_has_no_expiry(db):
    await _delegate(1)
    await delegate_registry.set_forced_guild(1, "OTHR", None)
    assert await delegate_registry.forced_guild(1) == "OTHR"


async def test_re_forcing_replaces_rather_than_stacking(db):
    """Two rows would mean the second `~force guild` silently did nothing."""
    await _delegate(1)
    await delegate_registry.set_forced_guild(1, "OTHR", _in(days=30))
    await delegate_registry.set_forced_guild(1, "THRD", _in(days=60))

    assert await ForceOverride.filter(kind="guild", subject="1").count() == 1
    assert await delegate_registry.forced_guild(1) == "THRD"


async def test_clearing_goes_back_to_the_watch(db):
    delegate = await _delegate(1, tag="VETS", currently="OTHR")
    await delegate_registry.set_forced_guild(1, "VETS", None)

    assert await delegate_registry.clear_forced_guild(1) == 1

    assert await delegate_registry.current_guild(delegate) == "OTHR"
    assert await delegate_registry.is_external(delegate) is True


async def test_clearing_nothing_reports_nothing(db):
    assert await delegate_registry.clear_forced_guild(999) == 0


async def test_the_override_is_per_member(db):
    theirs = await _delegate(2, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "OTHR", None)

    assert await delegate_registry.is_external(theirs) is False


async def test_the_forced_tag_folds_case(db):
    """`~force guild @them vets` has to mean the guild cached as VETS."""
    delegate = await _delegate(1, tag="VETS", currently="OTHR")
    await delegate_registry.set_forced_guild(1, "vets", None)

    assert await delegate_registry.is_external(delegate) is False
