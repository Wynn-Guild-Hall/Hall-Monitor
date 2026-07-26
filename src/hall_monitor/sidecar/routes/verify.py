"""Route called by picolimbo when a player types a chat line prefixed ``hall ``.

Response shape is contractual: ``{"kick_message": str | null}``. Picolimbo
disconnects the player with ``kick_message`` when it's non-null; otherwise
the chat line is silently accepted.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/verify/{uuid}/{msg:path}")
async def verify(uuid: str, msg: str) -> dict:
    return {"kick_message": None}
