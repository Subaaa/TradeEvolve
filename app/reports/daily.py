"""Daily report over the closed trades of one UTC day.

Takes persisted `TradeRecord` rows rather than the backtester's in-memory
`Trade`, which is what the previous version required — and which live trading
never produced, so the report could only ever describe a simulation.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from app.db.journal_store import SqliteJournalStore
from app.db.trades import TradeRecord, summarize


@dataclass(frozen=True)
class DailyReport:
    day: date
    net_pnl_usd: float
    num_trades: int
    num_rejections: int
    win_rate: float
    expectancy_r: float
    profit_factor: float
    max_drawdown_usd: float
    gross_fees_usd: float
    payload: dict[str, Any] = field(default_factory=dict)


def _intraday_drawdown(pnls: Sequence[float]) -> float:
    peak = 0.0
    running = 0.0
    worst = 0.0
    for p in pnls:
        running += p
        peak = max(peak, running)
        worst = max(worst, peak - running)
    return worst


def compute(
    *,
    day: date,
    trades: Sequence[TradeRecord],
    conn: sqlite3.Connection | None = None,
) -> DailyReport:
    """Summarise one day. `conn` is optional and only used for rejections."""
    ordered = sorted(
        trades, key=lambda t: t.closed_at or datetime.min.replace(tzinfo=UTC)
    )
    stats = summarize(ordered)
    pnls = [float(t.pnl_usd or 0.0) for t in ordered]

    rejections = 0
    if conn is not None:
        start = datetime.combine(day, time.min, tzinfo=UTC)
        end = start + timedelta(days=1)
        counts = SqliteJournalStore(conn).count_by_reason_since(start.isoformat())
        rejections = sum(v for k, v in counts.items() if k != "OK")
        del end

    pf = stats["profit_factor"]
    return DailyReport(
        day=day,
        net_pnl_usd=stats["net_pnl_usd"],
        num_trades=int(stats["count"]),
        num_rejections=rejections,
        win_rate=stats["win_rate"],
        expectancy_r=stats["expectancy_r"],
        profit_factor=pf if math.isfinite(pf) else 0.0,
        max_drawdown_usd=_intraday_drawdown(pnls),
        gross_fees_usd=sum(float(t.fees_usd or 0.0) for t in ordered),
        payload={
            "trades": [
                {
                    "id": t.id,
                    "side": t.side.value,
                    "units": t.units,
                    "entry": t.entry_fill_price,
                    "exit": t.exit_price,
                    "exit_reason": t.exit_reason.value if t.exit_reason else None,
                    "pnl_usd": t.pnl_usd,
                    "pnl_r": t.pnl_r,
                    "mfe_r": t.mfe_r,
                    "mae_r": t.mae_r,
                    "bars_held": t.bars_held,
                }
                for t in ordered
            ]
        },
    )


def save(conn: sqlite3.Connection, report: DailyReport) -> None:
    conn.execute(
        "INSERT INTO daily_reports(day, net_pnl, num_trades, num_rejections, payload)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(day) DO UPDATE SET net_pnl = excluded.net_pnl,"
        " num_trades = excluded.num_trades, num_rejections = excluded.num_rejections,"
        " payload = excluded.payload",
        (
            report.day.isoformat(),
            report.net_pnl_usd,
            report.num_trades,
            report.num_rejections,
            json.dumps(report.payload, default=str),
        ),
    )


def list_recent(conn: sqlite3.Connection, *, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM daily_reports ORDER BY day DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "day": r["day"],
            "net_pnl": r["net_pnl"],
            "num_trades": r["num_trades"],
            "num_rejections": r["num_rejections"],
        }
        for r in rows
    ]
