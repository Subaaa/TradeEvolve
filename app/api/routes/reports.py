"""Daily/weekly reports endpoints (stub)."""
from __future__ import annotations

from fastapi import APIRouter


router = APIRouter()


@router.get("/reports/daily")
def list_daily_reports() -> dict:
    return {"items": []}


@router.get("/reports/weekly")
def list_weekly_reviews() -> dict:
    return {"items": []}
