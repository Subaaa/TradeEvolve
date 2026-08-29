"""Prometheus-style metrics endpoint (stub)."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse


router = APIRouter()


@router.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return (
        "# HELP nito_trades_total Total number of executed trades.\n"
        "# TYPE nito_trades_total counter\n"
        "nito_trades_total 0\n"
        "# HELP nito_pnl_total_usd Total realized PnL in USD.\n"
        "# TYPE nito_pnl_total_usd counter\n"
        "nito_pnl_total_usd 0.0\n"
    )
