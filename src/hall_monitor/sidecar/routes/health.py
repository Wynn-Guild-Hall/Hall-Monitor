"""Liveness probe consumed by operators and container orchestration."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/health")
async def health() -> dict:
    return {"ok": True}
