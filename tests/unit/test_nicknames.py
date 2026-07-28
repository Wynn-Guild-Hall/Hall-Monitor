"""The `Username [TAG]` nickname shape, and who it applies to."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from hall_monitor.db.models import Delegate
from hall_monitor.services import nicknames

OBSERVER_ROLE_ID = 500


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.services.nicknames.settings.observer_role_id", OBSERVER_ROLE_ID
    )


def _member(user_id=1, *, nick=None, name="Tester", roles=(), bot=False):
    member = MagicMock()
    member.id = user_id
    member.bot = bot
    member.nick = nick
    member.name = name
    member.roles = [_role(role_id) for role_id in roles]
    member.edit = AsyncMock()
    return member


def _role(role_id):
    role = MagicMock()
    role.id = role_id
    return role


async def _delegate(user_id=1, *, tag="VETS", username="Holidaze", currently=None):
    return await Delegate.create(
        mc_uuid=f"uuid-{user_id}",
        discord_user_id=user_id,
        guild_tag=tag,
        mc_username=username,
        current_guild_tag=currently,
    )


def _new_nick(member) -> str:
    return member.edit.await_args.kwargs["nick"]


# --------------------------------------------------------------------------
# The shape
# --------------------------------------------------------------------------


def test_the_tag_is_appended_to_what_they_chose():
    assert nicknames.desired("Bob", "VETS") == "Bob [VETS]"


def test_an_existing_tag_is_replaced_not_stacked():
    assert nicknames.visible_part("Bob [OLD]") == "Bob"


def test_only_one_trailing_bracket_is_taken():
    """A name that genuinely ends in brackets keeps them."""
    assert nicknames.visible_part("Bob [he/him] [VETS]") == "Bob [he/him]"


def test_a_long_name_is_truncated_rather_than_the_tag():
    """The tag is the part that means something to everyone else."""
    result = nicknames.desired("W" * 40, "VETS")
    assert len(result) <= nicknames.NICK_LIMIT
    assert result.endswith(" [VETS]")


def test_an_empty_visible_part_leaves_just_the_tag():
    assert nicknames.desired("   ", "VETS") == "[VETS]"


def test_enforcement_is_idempotent():
    """Our own rename comes back as another member update; the second pass
    has to agree with the first or it never settles."""
    once = nicknames.desired(nicknames.visible_part("Bob"), "VETS")
    twice = nicknames.desired(nicknames.visible_part(once), "VETS")
    assert once == twice


# --------------------------------------------------------------------------
# Who it applies to
# --------------------------------------------------------------------------


async def test_a_new_delegate_is_named_from_their_minecraft_username(db):
    await _delegate(1, username="Holidaze")
    member = _member(1, nick=None)

    assert await nicknames.enforce(member) is True
    assert _new_nick(member) == "Holidaze [VETS]"


async def test_a_chosen_nickname_is_kept_and_tagged(db):
    await _delegate(1)
    member = _member(1, nick="Bob")

    await nicknames.enforce(member)

    assert _new_nick(member) == "Bob [VETS]"


async def test_dropping_the_tag_puts_it_back(db):
    await _delegate(1)
    member = _member(1, nick="Bob [SOMETHINGELSE]")

    await nicknames.enforce(member)

    assert _new_nick(member) == "Bob [VETS]"


async def test_a_correct_nickname_costs_no_request(db):
    """`on_member_update` fires once per role added — five times on a
    verification — so the settled path has to be free."""
    await _delegate(1)
    member = _member(1, nick="Bob [VETS]")

    assert await nicknames.enforce(member) is False
    member.edit.assert_not_awaited()


async def test_an_external_rep_wears_ext(db):
    await _delegate(1, tag="VETS", currently="OTHR")
    member = _member(1, nick="Bob [VETS]")

    await nicknames.enforce(member)

    assert _new_nick(member) == "Bob [EXT]"


async def test_observers_are_left_alone(db):
    """They're guests, not representatives — there's no guild to name."""
    await _delegate(1)
    member = _member(1, nick="Bob", roles=(OBSERVER_ROLE_ID,))

    assert await nicknames.enforce(member) is False
    member.edit.assert_not_awaited()


async def test_a_member_we_dont_have_on_file_is_left_alone(db):
    """Staff, guests, anyone who never verified. Renaming them would be the
    bot reaching well past what it was invited to do."""
    member = _member(99, nick="Someone")

    assert await nicknames.enforce(member) is False
    member.edit.assert_not_awaited()


async def test_bots_are_left_alone(db):
    await _delegate(1)
    member = _member(1, nick="Bob", bot=True)

    assert await nicknames.enforce(member) is False


async def test_a_rename_we_arent_allowed_to_make_is_reported_not_raised(db):
    """Server owners can't be renamed by anyone, bot or not."""
    await _delegate(1)
    member = _member(1, nick="Bob")
    member.edit = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "cannot rename owner")
    )

    assert await nicknames.enforce(member) is False


async def test_an_impossible_rename_is_only_reported_once(db, caplog):
    """The owner testing the bot is also a delegate, so every role change
    would otherwise log the same refusal forever."""
    nicknames.reset_unrenameable()
    await _delegate(1)
    member = _member(1, nick="Bob")
    member.edit = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "cannot rename owner")
    )

    with caplog.at_level("WARNING"):
        for _ in range(5):
            await nicknames.enforce(member)

    assert caplog.text.count("not allowed to rename") == 1
    assert member.edit.await_count == 5, "still tried — only the logging is throttled"
    nicknames.reset_unrenameable()


async def test_a_repointed_rep_is_named_for_the_guild_they_now_speak_for(db):
    """The tag and the colour have to agree, or the nickname tells the room
    something the member list contradicts."""
    from hall_monitor.services import delegate_registry

    await _delegate(1, tag="VETS")
    await delegate_registry.set_forced_guild(1, "ANO", None)
    member = _member(1, nick="Bob [VETS]")

    await nicknames.enforce(member)

    assert _new_nick(member) == "Bob [ANO]"
