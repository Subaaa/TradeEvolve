"""Read-only JSON API on 127.0.0.1.

Every route reads; none of them can place an order, clear the kill switch or
change a strategy. Those are CLI operations, deliberately, because they need a
person.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Query

from app.db import experiments as exp_db
from app.db import strategy_versions as sv_db
from app.db import system_state as ss
from app.db import trades as trades_db
from app.db.journal_store import SqliteJournalStore
from app.journal.types import JournalKind
from app.reports import daily as daily_report
from app.scheduler import deps


def create_app() -> FastAPI:
    app = FastAPI(title="TradeEvolve", version="0.2.0", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "ts": datetime.now(tz=UTC).isoformat()}

    @app.get("/system/state")
    def system_state() -> dict[str, Any]:
        conn = deps.db()
        kill = ss.load_kill_switch(conn)
        day = ss.load_day_anchor(conn)
        week = ss.load_week_anchor(conn)
        last_tick = ss.load_last_tick(conn)
        last_bar = ss.load_last_processed_bar_ts(conn)
        open_trade = trades_db.open_trade(conn)
        return {
            "kill_switch": kill.model_dump(mode="json"),
            "day_anchor": day.model_dump(mode="json") if day else None,
            "week_anchor": week.model_dump(mode="json") if week else None,
            "equity_high": ss.load_equity_high(conn, default=0.0),
            "last_tick": last_tick.isoformat() if last_tick else None,
            "last_processed_bar_ts": last_bar.isoformat() if last_bar else None,
            "broker_capabilities": ss.get(conn, ss.BROKER_CAPABILITIES),
            "open_trade": open_trade.model_dump(mode="json") if open_trade else None,
            "session_id": deps.SESSION_ID,
        }

    @app.get("/trades")
    def trades(
        limit: int = Query(default=50, ge=1, le=500),
        status: str | None = None,
    ) -> dict[str, Any]:
        conn = deps.db()
        rows = trades_db.list_recent(conn, limit=limit)
        if status:
            rows = [t for t in rows if t.status.value == status]
        return {
            "items": [t.model_dump(mode="json") for t in rows],
            "summary": trades_db.summarize(rows),
        }

    @app.get("/journal")
    def journal(
        limit: int = Query(default=50, ge=1, le=500),
        kind: str | None = None,
    ) -> dict[str, Any]:
        store = SqliteJournalStore(deps.db())
        parsed = JournalKind(kind) if kind else None
        entries = store.list_recent(limit=limit, kind=parsed)
        return {
            "items": [
                {
                    "id": e.id,
                    "ts": e.ts.isoformat(),
                    "kind": e.kind.value,
                    "next_state": e.next_state.value if e.next_state else None,
                    "reason_codes": e.reason_codes,
                    "payload": e.payload,
                }
                for e in entries
            ]
        }

    @app.get("/strategy/current")
    def strategy_current() -> dict[str, Any]:
        conn = deps.db()
        champion = sv_db.load_champion(conn)
        experiment = exp_db.active_experiment(conn)
        return {
            "champion": champion.model_dump(mode="json") if champion else None,
            "active_experiment": (
                experiment.model_dump(mode="json") if experiment else None
            ),
            "lineage": [
                {
                    "id": v.id,
                    "version_tag": v.version_tag,
                    "status": v.status.value,
                    "created_at": v.created_at.isoformat(),
                    "parent_version_id": v.parent_version_id,
                }
                for v in sv_db.lineage(conn, limit=25)
            ],
        }

    @app.get("/reports/daily")
    def reports_daily(limit: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
        return {"items": daily_report.list_recent(deps.db(), limit=limit)}

    return app
