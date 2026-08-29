"""Health endpoint."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter


router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }
