"""``~dash`` — a guild's answers to a fixed set of questions.

Contacts fill these in for the guild they represent, and a Hallway page
will render them. The keys are declared in ``services/dash_schema.py``
and cannot be invented here — see that module for why.

Which guild a value lands against is **the one the invoker represents**,
not the tag on their row: ``~force guild`` can repoint that, and using
the row would let somebody repointed to ANO carry on editing VETS's
page. Same rule ``~force assign`` learned in Stage 10.

**A bare ``~dash`` lists everything.** That isn't decoration — with keys
declared, the listing is the only way to discover what can be set, and a
command that refuses unknown keys without showing the known ones is
unusable. It doubles as the answer to "what does my guild currently
say?", which is the other question anybody has here.
"""

import logging

from discord.ext import commands

from hall_monitor.discord_bot.permissions import is_contact
from hall_monitor.services import dash, dash_schema, delegate_registry

logger = logging.getLogger(__name__)


class NoGuild(Exception):
    """The invoker doesn't speak for a guild, so there's nothing to edit."""


async def speaking_for(ctx: commands.Context) -> str:
    """The guild whose values this invoker may edit.

    Staff pass the contact gate by nesting, so a janitor with no
    ``Delegate`` row reaches here — and has no guild to write against.
    Refused rather than guessed at.
    """
    delegate = await delegate_registry.get_by_discord_user_id(ctx.author.id)
    if delegate is None or delegate.left_at is not None:
        raise NoGuild
    return await delegate_registry.represented_guild(delegate)


def unknown_key(name: str) -> str:
    """Refuse an undeclared key, naming the ones that exist.

    The list is the point. Somebody who can't invent a key has no other
    way to find out what they may set.
    """
    return (
        f"`{name}` isn't one of the Hall's questions. These are:\n"
        + "\n".join(
            f"· `{key.name}` — {key.description} (`~dash {key.command}`)"
            for key in dash_schema.KEYS.values()
        )
    )


def wrong_kind(exc: dash_schema.WrongKind) -> str:
    tried = "toggle" if exc.tried == dash_schema.BOOL else "set"
    return (
        f"`{exc.key.name}` takes `~dash {exc.key.command}`, not `~dash {tried}` "
        f"— {exc.key.description}."
    )


async def render_all(guild_tag: str) -> str:
    values = await dash.values_for(guild_tag)
    lines = [f"**`{guild_tag}`'s dashboard**"]
    for key in dash_schema.KEYS.values():
        lines.append(
            f"· `{key.name}`: {dash_schema.render(key, values.get(key.name))}"
            f" — {key.description}"
        )
    lines += [
        "",
        "`~dash toggle <key> yes|no` · `~dash set <key> <value>` · "
        "`~dash unset <key>`",
    ]
    return "\n".join(lines)


class Dash(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_command_error(
        self, ctx: commands.Context, error: Exception
    ) -> None:
        """Turn the schema's exceptions into something the invoker can act on.

        Handled on the cog rather than in each subcommand so one added
        later can't forget to — the same reasoning that put the roster
        redraw on ``ForceGroup.cog_after_invoke``. Anything else is left
        alone for the global handler.
        """
        original = getattr(error, "original", error)
        if isinstance(original, NoGuild):
            await ctx.reply(
                "I don't have you on file as representing a guild, so there's "
                "no dashboard for you to edit."
            )
        elif isinstance(original, dash_schema.WrongKind):
            await ctx.reply(wrong_kind(original))
        elif isinstance(original, dash_schema.UnknownKey):
            await ctx.reply(unknown_key(original.args[0]))
        else:
            return  # not ours

    @commands.group(name="dash", invoke_without_command=True)
    @is_contact()
    async def dash(self, ctx: commands.Context) -> None:
        """see and edit your guild's entry"""
        await ctx.reply(await render_all(await speaking_for(ctx)))

    @dash.command(name="keys")
    @is_contact()
    async def keys(self, ctx: commands.Context) -> None:
        """list the questions your guild can answer"""
        await ctx.reply(await render_all(await speaking_for(ctx)))

    @dash.command(name="toggle")
    @is_contact()
    async def toggle(self, ctx: commands.Context, key: str, value: str) -> None:
        """answer a yes/no question"""
        guild_tag = await speaking_for(ctx)
        declared = dash_schema.require(key, dash_schema.BOOL)
        try:
            parsed = dash_schema.parse_bool(value)
        except dash_schema.BadValue:
            await ctx.reply(f"`{value}` isn't a yes or a no — try `yes` or `no`.")
            return
        await dash.write(guild_tag, declared, parsed)
        logger.info(
            "dash: %s set %s/%s to %s", ctx.author.id, guild_tag, declared.name, parsed
        )
        await ctx.reply(
            f"`{guild_tag}`'s `{declared.name}` is now "
            f"**{'yes' if parsed else 'no'}**."
        )

    @dash.command(name="set")
    @is_contact()
    async def set_(self, ctx: commands.Context, key: str, *, value: str = "") -> None:
        """answer a question that takes text"""
        guild_tag = await speaking_for(ctx)
        declared = dash_schema.require(key, dash_schema.SCALAR)
        try:
            cleaned = dash_schema.clean_scalar(declared, value)
        except dash_schema.BadValue:
            # Refused rather than truncated: a value silently cut at the
            # limit is worse than one that didn't save, because nothing
            # tells the author the end of their sentence has gone.
            await ctx.reply(
                f"that's {len(value.strip())} characters and the limit is "
                f"{declared.max_length}."
                if value.strip()
                else "give me something to set it to, or `~dash unset` it."
            )
            return
        await dash.write(guild_tag, declared, cleaned)
        logger.info("dash: %s set %s/%s", ctx.author.id, guild_tag, declared.name)
        await ctx.reply(f"`{guild_tag}`'s `{declared.name}` is now `{cleaned}`.")

    @dash.command(name="unset")
    @is_contact()
    async def unset(self, ctx: commands.Context, key: str) -> None:
        """clear an answer, back to how an unfilled guild reads"""
        guild_tag = await speaking_for(ctx)
        declared = dash_schema.get(key)
        if not await dash.clear(guild_tag, declared):
            await ctx.reply(
                f"`{guild_tag}`'s `{declared.name}` was already unset — "
                "nothing to clear."
            )
            return
        logger.info("dash: %s unset %s/%s", ctx.author.id, guild_tag, declared.name)
        await ctx.reply(f"`{guild_tag}`'s `{declared.name}` is unset again.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Dash(bot))
