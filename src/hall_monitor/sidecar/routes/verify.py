"""Route called by picolimbo when a player types a chat line starting ``hall``.

Response shape is contractual: ``{"kick_message": str | null, "chat_message":
str | null}``. Picolimbo disconnects the player with ``kick_message`` when
it's non-null, otherwise sends ``chat_message`` back in chat, otherwise
silently accepts the line.

**Only success disconnects.** The invite has to survive the end of the
session, and the disconnect screen is the one place text stays put long
enough to copy. Every rejection answers in chat instead — being kicked
for a mistyped code means reconnecting just to try again.

Picolimbo strips the route prefix, so ``msg`` is normally just the digits
of a ``HALL<NN>`` code.

The eligibility check re-runs here because the Hallway lookup is only
advisory — someone can post any UUID via a curl. The MC-time check is
authoritative.
"""

import logging
import time

from fastapi import APIRouter, Request

from hall_monitor.config import settings
from hall_monitor.external import wynncraft
from hall_monitor.services import (
    delegate_registry,
    discord_invites,
    expel,
    mc_command,
    major_guilds,
    role_bits,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _chat(message: str) -> dict:
    """Answer in chat and leave the player connected."""
    return {"kick_message": None, "chat_message": message}


def _kick(message: str) -> dict:
    """Disconnect, putting ``message`` on the disconnect screen."""
    return {"kick_message": message, "chat_message": None}


def _ignore() -> dict:
    return {"kick_message": None, "chat_message": None}


def _welcome(guild_tag: str, invite_code: str) -> str:
    """The one message a player has to act on after being disconnected.

    Nothing on a Minecraft disconnect screen is clickable, so the code
    comes first and on its own line — that's what gets typed into
    Discord's join dialog. The full URL follows for anyone who'd rather
    paste a link than find that dialog.

    Picolimbo renders kick reasons as MiniMessage (auth-stack patch 0005),
    so the code can be picked out in colour from the instructions around
    it. Tags are limited to the vanilla colour names and ``bold``. An
    older picolimbo would print these literally, so this needs the newer
    image deployed first.
    """
    return (
        f"<gold>Welcome, <bold>{guild_tag}</bold> representative.</gold>\n\n"
        f"<gray>Invite code:</gray>  <green><bold>{invite_code}</bold></green>\n"
        f"<gray>In Discord: + (Add a Server) > Join a Server > paste the code</gray>\n\n"
        f"<gray>Or open</gray>  <aqua>https://discord.gg/{invite_code}</aqua>"
    )


@router.get("/api/verify/{uuid}/{msg:path}")
async def verify(request: Request, uuid: str, msg: str) -> dict:
    """Log one line per request, whatever the outcome.

    A success used to log nothing at all, which made "it disconnected me
    without the invite" unanswerable after the fact: nothing on our side
    recorded whether we'd sent a kick, a chat line, or neither. The
    elapsed time is here for the same reason — the forward runs inside
    picolimbo's packet handler, so how long we take is the player's
    connection not being serviced.
    """
    started = time.perf_counter()
    outcome, why = await _decide(request, uuid, msg)
    logger.log(
        logging.DEBUG if why == "ignored" else logging.INFO,
        "verify: %s %r -> %s in %.0fms",
        uuid,
        msg,
        why,
        (time.perf_counter() - started) * 1000,
    )
    return outcome


async def _decide(request: Request, uuid: str, msg: str) -> tuple[dict, str]:
    """The response, plus a short label for the log line."""
    try:
        command = mc_command.parse(msg)
    except mc_command.InvalidCode:
        if not mc_command.looks_like_attempt(msg):
            # Ordinary chat that happens to start with "hall" — the route
            # prefix is that broad. Say nothing at all.
            return _ignore(), "ignored"
        return (
            _chat(
                "That isn't a Guild Hall code. Get yours at hall.wynnvets.org/join"
            ),
            "chat: unparseable code",
        )

    try:
        role_bits.decode(command.bits)  # validates; we forward the raw bits to mint
    except role_bits.UnknownRoleBit:
        return (
            _chat(
                "That code asks for a role we don't recognise. "
                "Get a fresh one at hall.wynnvets.org/join"
            ),
            "chat: unknown role bit",
        )

    player = await wynncraft.get_player(uuid, urgent=True)
    player_guild = player.guild if player else None
    if player_guild is None or player_guild.rank not in delegate_registry.ELIGIBLE_RANKS:
        return (
            _chat("You aren't chief or owner of a major guild."),
            f"chat: not eligible (rank={player_guild.rank if player_guild else None})",
        )

    # Before the major-guild check, deliberately. An expelled guild can be perfectly
    # major — expulsion is about welcome, not significance — and telling
    # a chief their guild "isn't major" when the Hall voted it out sends
    # them off chasing leaderboards.
    if await expel.is_banned(player_guild.prefix):
        return (
            _chat(
                f"{player_guild.prefix} is barred from the Guild Hall. "
                "Speak to a Hall monitor."
            ),
            f"chat: {player_guild.prefix} expelled",
        )

    if not await major_guilds.is_major(player_guild.prefix):
        return (
            _chat(f"{player_guild.prefix} isn't a major guild."),
            f"chat: {player_guild.prefix} not major",
        )

    bot = getattr(request.app.state, "bot", None)
    discord_guild = None
    welcome_channel = None
    if bot is not None and settings.discord_guild_id:
        discord_guild = bot.get_guild(settings.discord_guild_id)
        if discord_guild is not None and settings.welcome_channel_id:
            welcome_channel = discord_guild.get_channel(settings.welcome_channel_id)

    if welcome_channel is None:
        logger.error("verify: welcome channel unavailable; can't mint invite")
        return (
            _chat("Verification is temporarily unavailable. Try again in a minute."),
            "chat: no welcome channel",
        )

    try:
        pending = await discord_invites.mint_invite(
            uuid,
            player_guild.prefix,
            command.bits,
            channel=welcome_channel,
            bot=bot,
            discord_guild=discord_guild,
            mc_username=player.username if player else None,
        )
    except discord_invites.AlreadyLiveDelegate:
        return (
            _chat(
                "You're already verified in the Guild Hall. Ask a janitor if "
                "you've lost your roles."
            ),
            "chat: already a live delegate",
        )
    except discord_invites.AlreadyObserving:
        # An invite would be worse than useless here: they're already in
        # the server, so clicking it fires no join event and nothing at
        # all happens. Say what to ask for instead.
        return (
            _chat(
                f"You're already in the Guild Hall as an observer. Ask a "
                f"janitor to make you {player_guild.prefix}'s representative "
                "— they'll need to run `~force rep` for you."
            ),
            "chat: observer asking to represent",
        )
    except Exception:
        logger.exception("verify: mint_invite failed for %s", uuid)
        return (
            _chat("Verification is temporarily unavailable. Try again in a minute."),
            "chat: mint failed",
        )

    return (
        _kick(_welcome(player_guild.prefix, pending.discord_invite_code)),
        f"kick: invite {pending.discord_invite_code} for {player_guild.prefix}",
    )
