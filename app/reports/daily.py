"""Daily report: a deterministic function over the day's journal rows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

from app.evolution.backtester import Trade
from app.llm.schemas import DailyReportSummary


@dataclass(frozen=True)
class DailyReport:
    day: date
    net_pnl_usd: float
    num_trades: int
    num_rejections: int
    sharpe: float
    max_drawdown_pct: float
    payload: dict


def compute(
    *,
    day: date,
    trades: Iterable[Trade],
    journal_rejections: int,
) -> DailyReport:
    """Compute a daily report from the day's trades and the rejection count."""
    pnls = [t.pnl_usd for t in trades]
    net = sum(pnls)
    num = len(pnls)
    # Sharpe over single-day trades: sqrt(num) is the annualization we
    # use elsewhere; for a single day we just record the mean / std.
    if num >= 2:
        mean = sum(pnls) / num
        var = sum((p - mean) ** 2 for p in pnls) / (num - 1)
        std = var ** 0.5
        sharpe = mean / std if std > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown over the day's equity curve.
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    return DailyReport(
        day=day,
        net_pnl_usd=net,
        num_trades=num,
        num_rejections=journal_rejections,
        sharpe=sharpe,
        max_drawdown_pct=max_dd,
        payload={
            "day": day.isoformat(),
            "trades": [
                {
                    "side": t.side.value,
                    "units": t.units,
                    "entry": t.entry,
                    "exit": t.exit_price,
                    "pnl_usd": t.pnl_usd,
                    "pnl_r": t.pnl_r,
                    "exit_reason": t.exit_reason,
                }
                for t in trades
            ],
            "sharpe": sharpe,
            "max_drawdown_pct": max_dd,
        },
    )


def to_llm_summary(r: DailyReport) -> DailyReportSummary:
    return DailyReportSummary(
        day=r.day.isoformat(),
        net_pnl_usd=r.net_pnl_usd,
        num_trades=r.num_trades,
        num_rejections=r.num_rejections,
        sharpe=r.sharpe,
        max_drawdown_pct=r.max_drawdown_pct,
    )
