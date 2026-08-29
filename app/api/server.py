"""FastAPI app factory.

The API binds to 127.0.0.1:<port> only. An optional nginx (in front)
terminates TLS and forwards here.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api.routes import health, journal, metrics, reports, strategy, system, trades


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="nito-trade-research",
        version="0.1.0",
        description="Read-only JSON API for the paper-trading research agent.",
    )
    app.include_router(health.router, tags=["health"])
    app.include_router(system.router, tags=["system"])
    app.include_router(journal.router, tags=["journal"])
    app.include_router(trades.router, tags=["trades"])
    app.include_router(reports.router, tags=["reports"])
    app.include_router(strategy.router, tags=["strategy"])
    app.include_router(metrics.router, tags=["metrics"])
    return app
