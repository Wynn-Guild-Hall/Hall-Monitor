"""`~help` / bare-group listings, and the command-error handler that keeps a
stub from reaching Discord as a traceback."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands

from hall_monitor.discord_bot import command_help, errors

ALLOWED = 1
DENIED = 2


def _allowed_only():
    return commands.check(lambda ctx: ctx.author.id == ALLOWED)


@commands.command(name="live")
async def live(ctx, user: str, kind: str) -> None:
    """does a live thing"""


@commands.command(name="bare")
async def bare(ctx) -> None:
    """takes nothing"""


@commands.command(name="stub", hidden=True)
async def stub(ctx) -> None:
    """not built yet"""


@commands.command(name="gated")
@_allowed_only()
async def gated(ctx) -> None:
    """needs the role"""


@commands.command(name="composite")
@commands.check_any(_allowed_only())
async def composite(ctx) -> None:
    """gated by a check that raises rather than returning False"""


@commands.group(name="grp", invoke_without_command=True)
async def grp(ctx) -> None:
    """a group"""


@grp.command(name="open")
async def grp_open(ctx, thing: str) -> None:
    """open subcommand"""


@grp.command(name="shut")
@_allowed_only()
async def grp_shut(ctx) -> None:
    """gated subcommand"""


@commands.group(name="locked", invoke_without_command=True)
async def locked(ctx) -> None:
    """a group with nothing in it for most callers"""


@locked.command(name="inner")
@_allowed_only()
async def locked_inner(ctx) -> None:
    """gated subcommand"""


def _ctx(user_id: int = ALLOWED, command=None):
    ctx = MagicMock()
    ctx.clean_prefix = "~"
    ctx.guild = MagicMock()
    ctx.author.id = user_id
    ctx.command = command
    ctx.bot.can_run = AsyncMock(return_value=True)
    ctx.reply = AsyncMock()
    return ctx


def _parent(*cmds):
    return SimpleNamespace(commands=list(cmds))


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_signature_includes_the_prefix_and_params():
    assert command_help.signature(_ctx(), live) == "`~live <user> <kind>`"


def test_signature_of_an_argless_command_has_no_trailing_space():
    assert command_help.signature(_ctx(), bare) == "`~bare`"


def test_signature_of_a_subcommand_is_qualified():
    assert command_help.signature(_ctx(), grp_open) == "`~grp open <thing>`"


async def test_visible_skips_unbuilt_commands():
    """`hidden=True` is the single marker for not-built-yet."""
    shown = await command_help.visible(_ctx(), _parent(live, stub))
    assert [c.name for c in shown] == ["live"]


async def test_visible_skips_commands_the_caller_cant_run():
    shown = await command_help.visible(_ctx(user_id=DENIED), _parent(live, gated))
    assert [c.name for c in shown] == ["live"]


async def test_visible_handles_a_check_that_raises():
    """`check_any` raises instead of returning False; a listing treats the
    two identically or it advertises commands that error on use."""
    shown = await command_help.visible(_ctx(user_id=DENIED), _parent(composite))
    assert shown == []


async def test_render_all_indents_subcommands_under_their_group():
    body = await command_help.render_all(_ctx(), _parent(grp))
    assert "· `~grp` — a group" in body
    assert "    · `~grp open <thing>` — open subcommand" in body


async def test_render_all_omits_a_group_with_nothing_runnable_in_it():
    """A `~force` line with no subcommands under it reads as a command the
    caller can run, and it isn't one."""
    body = await command_help.render_all(_ctx(user_id=DENIED), _parent(locked, live))
    assert "~locked" not in body
    assert "~live" in body


async def test_render_all_falls_back_when_nothing_is_available():
    body = await command_help.render_all(_ctx(user_id=DENIED), _parent(gated))
    assert "hall.wynnvets.org/join" in body


async def test_render_group_lists_what_the_caller_can_run():
    body = await command_help.render_group(_ctx(), grp)
    assert "`~grp open <thing>`" in body
    assert "`~grp shut`" in body


async def test_render_group_hides_gated_subcommands():
    body = await command_help.render_group(_ctx(user_id=DENIED), grp)
    assert "shut" not in body
    assert "`~grp open <thing>`" in body


async def test_render_group_says_so_when_you_can_run_none_of_it():
    body = await command_help.render_group(_ctx(user_id=DENIED), locked)
    assert body == "there's no `~locked` subcommand you can run."


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------


async def test_unbuilt_command_answers_instead_of_raising():
    """The bug this whole file exists for: `~force` used to reach Discord as
    a NotImplementedError traceback and nothing else."""
    ctx = _ctx(command=grp)
    await errors.handle(ctx, commands.CommandInvokeError(NotImplementedError()))
    ctx.reply.assert_awaited_once()
    assert "isn't built yet" in ctx.reply.await_args.args[0]


async def test_unbuilt_command_says_so_even_when_the_gate_rejected_first():
    """A check runs before the body, so an unbuilt *gated* command would
    otherwise report a missing role — which is what `~dash` told a monitor."""
    ctx = _ctx(command=stub)
    await errors.handle(ctx, commands.CheckFailure())
    assert "isn't built yet" in ctx.reply.await_args.args[0]


async def test_missing_role_says_so():
    ctx = _ctx(command=gated)
    await errors.handle(ctx, commands.CheckFailure())
    assert "role" in ctx.reply.await_args.args[0]


async def test_check_any_failure_is_treated_as_a_missing_role():
    ctx = _ctx(command=composite)
    await errors.handle(ctx, commands.CheckAnyFailure([], []))
    assert "role" in ctx.reply.await_args.args[0]


async def test_bad_input_replies_with_the_usage_line():
    ctx = _ctx(command=live)
    param = live.clean_params["kind"]
    await errors.handle(ctx, commands.MissingRequiredArgument(param))
    assert ctx.reply.await_args.args[0] == "usage: `~live <user> <kind>`"


async def test_unknown_command_stays_quiet():
    """`~` opens plenty of ordinary sentences."""
    ctx = _ctx()
    await errors.handle(ctx, commands.CommandNotFound())
    ctx.reply.assert_not_awaited()


async def test_unexpected_error_is_logged_and_owned_up_to(caplog):
    ctx = _ctx(command=live)
    with caplog.at_level(logging.ERROR):
        await errors.handle(ctx, commands.CommandInvokeError(ValueError("boom")))
    assert "logs" in ctx.reply.await_args.args[0]
    assert "boom" in caplog.text


async def test_a_reply_that_fails_doesnt_raise():
    """The handler is the last line — an exception here has nowhere to go."""
    ctx = _ctx(command=live)
    ctx.reply = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "no perms")
    )
    await errors.handle(ctx, commands.CheckFailure())


# --------------------------------------------------------------------------
# The real command tree
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module,name",
    [
        ("hall_monitor.discord_bot.cogs.general.dash", "dash"),
        ("hall_monitor.discord_bot.cogs.moderation.echo", "echo"),
        ("hall_monitor.discord_bot.cogs.moderation.embed", "embed"),
    ],
)
def test_unbuilt_commands_are_marked_hidden(module, name):
    """Whatever still raises NotImplementedError stays out of `~help`."""
    import importlib

    cog_class = next(
        obj
        for obj in vars(importlib.import_module(module)).values()
        if isinstance(obj, type) and issubclass(obj, commands.Cog) and obj is not commands.Cog
    )
    command = next(c for c in cog_class.__cog_commands__ if c.name == name)
    assert command.hidden is True
