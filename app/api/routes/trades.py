"""Trades listing endpoint (stub)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query


router = APIRouter()


@router.get("/trades")
def list_trades(
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict:
    return {"items": [], "limit": limit}
