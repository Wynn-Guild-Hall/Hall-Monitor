"""Read-only eligibility lookup called by the Hallway /join page.

No persistence — this endpoint only decides what UI the site should render.
The authoritative eligibility check re-runs at MC-time in ``verify.py``.
"""

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/join/lookup")
async def lookup(username: str) -> dict:
    raise HTTPException(status_code=501, detail="not implemented")
