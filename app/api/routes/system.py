"""System state endpoint (stub)."""
from __future__ import annotations

from fastapi import APIRouter


router = APIRouter()


@router.get("/system/state")
def system_state() -> dict:
    return {"kill_switch": False, "last_tick": None}
