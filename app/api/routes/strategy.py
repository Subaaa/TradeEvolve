"""Strategy endpoints (stub)."""
from __future__ import annotations

from fastapi import APIRouter


router = APIRouter()


@router.get("/strategy/current")
def current() -> dict:
    return {"champion": None, "challenger": None}


@router.get("/strategy/lineage")
def lineage(version_id: int) -> dict:
    return {"version_id": version_id, "ancestry": []}
