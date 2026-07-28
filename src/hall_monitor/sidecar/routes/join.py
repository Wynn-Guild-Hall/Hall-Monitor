"""Read-only eligibility lookup called by the Hallway /join page.

No persistence — this endpoint only decides what UI the site should render.
The authoritative eligibility check re-runs at MC-time in ``verify.py``.

Response shape
--------------
Eligible::

    {
      "eligible": true,
      "mc_username": "Notch",
      "guild_tag": "VETS",
      "current_contacts_per_role": {
        "events": null,
        "housing": "Notch",
        "warring": null,
        "ownership": null
      }
    }

Ineligible::

    {
      "eligible": false,
      "reason": "not chief or owner" | "guild not notable",
      "mc_username": "Notch",
      "guild_tag": "OTHR" | null
    }

Unknown username → HTTP 404.
"""

from fastapi import APIRouter, HTTPException, Request

from hall_monitor.config import settings
from hall_monitor.external import resolve_profile, wynncraft
from hall_monitor.services import contacts, delegate_registry, notability

router = APIRouter()


@router.get("/api/join/lookup")
async def lookup(request: Request, username: str) -> dict:
    profile = await resolve_profile(username, urgent=True)
    if profile is None:
        raise HTTPException(status_code=404, detail="username not found")

    player_guild = await wynncraft.get_player_guild(profile.uuid, urgent=True)
    if player_guild is None or player_guild.rank not in delegate_registry.ELIGIBLE_RANKS:
        return {
            "eligible": False,
            "reason": "not chief or owner",
            "mc_username": profile.username,
            "guild_tag": player_guild.prefix if player_guild else None,
        }

    if not await notability.is_notable(player_guild.prefix):
        return {
            "eligible": False,
            "reason": "guild not notable",
            "mc_username": profile.username,
            "guild_tag": player_guild.prefix,
        }

    bot = getattr(request.app.state, "bot", None)
    discord_guild = None
    if bot is not None and settings.discord_guild_id:
        discord_guild = bot.get_guild(settings.discord_guild_id)
    holders = await contacts.current_contacts_for_guild(
        player_guild.prefix, discord_guild=discord_guild
    )
    # Names, not UUIDs — this feeds a sentence a human reads before
    # deciding to take someone's role off them.
    return {
        "eligible": True,
        "mc_username": profile.username,
        "guild_tag": player_guild.prefix,
        "current_contacts_per_role": {
            role: (await delegate_registry.display_name(delegate) if delegate else None)
            for role, delegate in holders.items()
        },
    }
