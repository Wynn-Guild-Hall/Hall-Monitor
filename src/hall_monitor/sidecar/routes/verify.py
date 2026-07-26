"""Route called by picolimbo when a player types a chat line starting ``hall``.

Response shape is contractual: ``{"kick_message": str | null}``. Picolimbo
disconnects the player with ``kick_message`` when it's non-null; otherwise
the chat line is silently accepted.

Picolimbo strips the route prefix, so ``msg`` is normally just the digits
of a ``HALL<NN>`` code.

The eligibility check re-runs here because the Hallway lookup is only
advisory — someone can post any UUID via a curl. The MC-time check is
authoritative.
"""

import logging

from fastapi import APIRouter, Request

from hall_monitor.config import settings
from hall_monitor.external import wynncraft
from hall_monitor.services import (
    discord_invites,
    mc_command,
    notability,
    role_bits,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_ELIGIBLE_RANKS = frozenset({"OWNER", "CHIEF"})


@router.get("/api/verify/{uuid}/{msg:path}")
async def verify(request: Request, uuid: str, msg: str) -> dict:
    try:
        command = mc_command.parse(msg)
    except mc_command.InvalidCode:
        if not mc_command.looks_like_attempt(msg):
            # Ordinary chat that happens to start with "hall" — the route
            # prefix is that broad. Not worth a disconnect.
            return {"kick_message": None}
        return {
            "kick_message": (
                "That isn't a Guild Hall code. Get yours at hall.wynnvets.org/join"
            )
        }

    try:
        role_bits.decode(command.bits)  # validates; we forward the raw bits to mint
    except role_bits.UnknownRoleBit:
        return {
            "kick_message": (
                "That code asks for a role we don't recognise. "
                "Get a fresh one at hall.wynnvets.org/join"
            )
        }

    player_guild = await wynncraft.get_player_guild(uuid, urgent=True)
    if player_guild is None or player_guild.rank not in _ELIGIBLE_RANKS:
        return {"kick_message": "You aren't chief or owner of a notable guild."}

    if not await notability.is_notable(player_guild.prefix):
        return {"kick_message": f"{player_guild.prefix} isn't a notable guild."}

    bot = getattr(request.app.state, "bot", None)
    discord_guild = None
    welcome_channel = None
    if bot is not None and settings.discord_guild_id:
        discord_guild = bot.get_guild(settings.discord_guild_id)
        if discord_guild is not None and settings.welcome_channel_id:
            welcome_channel = discord_guild.get_channel(settings.welcome_channel_id)

    if welcome_channel is None:
        logger.error("verify: welcome channel unavailable; can't mint invite")
        return {"kick_message": "Verification is temporarily unavailable. Try again in a minute."}

    try:
        pending = await discord_invites.mint_invite(
            uuid,
            player_guild.prefix,
            command.bits,
            channel=welcome_channel,
            bot=bot,
            discord_guild=discord_guild,
        )
    except discord_invites.AlreadyLiveDelegate:
        return {"kick_message": "You're already verified in the Guild Hall."}
    except Exception:
        logger.exception("verify: mint_invite failed for %s", uuid)
        return {"kick_message": "Verification is temporarily unavailable. Try again in a minute."}

    return {
        "kick_message": (
            f"Welcome, {player_guild.prefix} representative. "
            f"Click to join the Guild Hall: https://discord.gg/{pending.discord_invite_code}"
        )
    }
