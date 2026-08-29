"""Journal listing endpoint.

This is a stub that documents the contract. A real implementation
would query `journal_entries` via the admin engine.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query


router = APIRouter()


@router.get("/journal")
def list_journal(
    kind: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict:
    return {"items": [], "limit": limit, "kind": kind}
