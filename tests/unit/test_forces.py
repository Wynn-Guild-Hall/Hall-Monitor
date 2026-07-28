"""`~force guild` — repointing which guild a member represents.

The override doesn't say "they're playing over there", it says "they
speak for this guild now", and everything keys off that single answer:
the tag on their nickname, the colour they wear, which slots they may
hold, and whose notability decides their standing.
"""

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


async def test_the_override_repoints_who_they_represent(db):
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    assert await delegate_registry.represented_guild(delegate) == "VETS"

    await delegate_registry.set_forced_guild(1, "ANO", _in(days=30))

    assert await delegate_registry.represented_guild(delegate) == "ANO"


async def test_a_forced_representative_is_never_external(db):
    """The watch seeing a different guild is the exact situation the
    janitor overrode — relegating them for it would make the override
    useless in the case it exists for."""
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "ANO", _in(days=30))

    assert await delegate_registry.is_external(delegate) is False
    assert (
        await delegate_registry.standing(delegate, notable=True)
        == delegate_registry.DELEGATE
    )


async def test_the_override_can_undo_a_wrong_external(db):
    """The other common case: a rep mid-transfer, or an alt showing the
    wrong guild. Forcing the guild they already represent puts them back."""
    delegate = await _delegate(1, tag="VETS", currently="OTHR")
    assert await delegate_registry.is_external(delegate) is True

    await delegate_registry.set_forced_guild(1, "VETS", _in(days=30))

    assert await delegate_registry.is_external(delegate) is False
    assert (
        await delegate_registry.standing(delegate, notable=True)
        == delegate_registry.DELEGATE
    )


async def test_drifting_off_unforced_is_still_external(db):
    delegate = await _delegate(1, tag="VETS", currently="OTHR")
    assert (
        await delegate_registry.standing(delegate, notable=True)
        == delegate_registry.EXTERNAL
    )
    assert await delegate_registry.represented_guild(delegate) == "VETS"


async def test_the_override_beats_the_watch_not_the_other_way(db):
    """The watch rewrites `current_guild_tag` hourly, so an override stored
    in that column would survive exactly one sweep."""
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "ANO", None)

    delegate.current_guild_tag = "VETS"  # a watch pass writing what it sees
    await delegate.save()

    assert await delegate_registry.represented_guild(delegate) == "ANO"


async def test_an_expired_override_stops_counting(db):
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "ANO", _in(days=-1))

    assert await delegate_registry.represented_guild(delegate) == "VETS"
    assert await ForceOverride.filter(kind="guild").count() == 1, (
        "left in place so a janitor can see what ran out"
    )


async def test_a_permanent_override_has_no_expiry(db):
    await _delegate(1)
    await delegate_registry.set_forced_guild(1, "ANO", None)
    assert await delegate_registry.forced_guild(1) == "ANO"


async def test_re_forcing_replaces_rather_than_stacking(db):
    """Two rows would mean the second `~force guild` silently did nothing."""
    await _delegate(1)
    await delegate_registry.set_forced_guild(1, "ANO", _in(days=30))
    await delegate_registry.set_forced_guild(1, "THRD", _in(days=60))

    assert await ForceOverride.filter(kind="guild", subject="1").count() == 1
    assert await delegate_registry.forced_guild(1) == "THRD"


async def test_clearing_goes_back_to_the_verified_guild(db):
    delegate = await _delegate(1, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "ANO", None)

    assert await delegate_registry.clear_forced_guild(1) == 1

    assert await delegate_registry.represented_guild(delegate) == "VETS"
    assert await delegate_registry.is_external(delegate) is False


async def test_clearing_nothing_reports_nothing(db):
    assert await delegate_registry.clear_forced_guild(999) == 0


async def test_the_override_is_per_member(db):
    theirs = await _delegate(2, tag="VETS", currently="VETS")
    await delegate_registry.set_forced_guild(1, "ANO", None)

    assert await delegate_registry.represented_guild(theirs) == "VETS"


async def test_the_forced_tag_folds_case(db):
    """`~force guild @them vets` has to mean the guild cached as VETS."""
    delegate = await _delegate(1, tag="VETS", currently="OTHR")
    await delegate_registry.set_forced_guild(1, "vets", None)

    assert await delegate_registry.is_external(delegate) is False
