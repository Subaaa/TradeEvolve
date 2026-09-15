"""Turn a week of trades and journal entries into the weekly review's input.

This is the only place that decides what evidence the model gets to see. Two
choices matter:

*   **Rejection counts are included.** If the binding constraint on the week
    was a discipline rule rather than the strategy, the model should be able
    to see that and say so, instead of proposing a parameter change that could
    not have helped.
*   **Excursions are separated by outcome.** Average MAE on winners and
    average MFE on losers are the two numbers that actually speak to whether
    the stop and the target are in the right place; the pooled averages hide
    both.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from app.db import experiments as exp_db
from app.db import trades as trades_db
from app.db.journal_store import SqliteJournalStore
from app.db.strategy_versions import StrategyVersion
from app.db.trades import TradeRecord
from app.llm.schemas import DayStats, ParamRange, PriorHypothesis, WeeklyReviewIn
from app.strategy.configs import LLM_RANGES, MUTABLE_BY_LLM

from .p_hacking import PlateauState


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_weekly_stats(
    conn: sqlite3.Connection,
    *,
    champion: StrategyVersion,
    now: datetime | None = None,
    days: int = 7,
    plateau: PlateauState | None = None,
    trade_reviews: list[str] | None = None,
) -> WeeklyReviewIn:
    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    start = moment - timedelta(days=days)

    closed = trades_db.list_closed(conn, since=start, until=moment, limit=1000)
    summary = trades_db.summarize(closed)

    winners = [t for t in closed if (t.pnl_r or 0.0) > 0]
    losers = [t for t in closed if (t.pnl_r or 0.0) <= 0]

    exit_counts: dict[str, int] = {}
    for t in closed:
        if t.exit_reason is not None:
            key = t.exit_reason.value
            exit_counts[key] = exit_counts.get(key, 0) + 1

    journal = SqliteJournalStore(conn)
    rejections = journal.count_by_reason_since(start.isoformat())

    by_day = _by_day(closed, start=start, end=moment)
    by_hour = _win_rate_by_hour(closed)

    params = {
        name: ParamRange(
            value=float(getattr(champion.config, name)),
            min=LLM_RANGES[name][0],
            max=LLM_RANGES[name][1],
        )
        for name in sorted(MUTABLE_BY_LLM)
    }

    priors = [
        PriorHypothesis(
            field=h.field,
            from_value=h.from_value,
            to_value=h.to_value,
            outcome="accepted" if h.accepted else "rejected",
            reason=h.reject_reason or (h.reason or ""),
        )
        for h in exp_db.hypotheses_for_champion(conn, champion.id, limit=10)
    ]

    return WeeklyReviewIn(
        period_start=start.isoformat(),
        period_end=moment.isoformat(),
        strategy_version_tag=champion.version_tag,
        params=params,
        total_trades=int(summary["count"]),
        net_pnl_usd=summary["net_pnl_usd"],
        expectancy_r=summary["expectancy_r"],
        win_rate=summary["win_rate"],
        profit_factor=summary["profit_factor"],
        # MAE on the trades that worked, MFE on the ones that did not: the two
        # figures that say whether the stop and target are placed sensibly.
        avg_mae_r=_mean([float(t.mae_r or 0.0) for t in winners]),
        avg_mfe_r=_mean([float(t.mfe_r or 0.0) for t in losers]),
        exit_reason_counts=exit_counts,
        rejection_counts=rejections,
        by_day=by_day,
        win_rate_by_hour=by_hour,
        prior_hypotheses=priors,
        plateau=bool(plateau.is_plateau) if plateau else False,
        trade_reviews=list(trade_reviews or []),
    )


def _by_day(
    closed: list[TradeRecord], *, start: datetime, end: datetime
) -> list[DayStats]:
    buckets: dict[str, list[TradeRecord]] = {}
    cursor = start.date()
    while cursor <= end.date():
        buckets[cursor.isoformat()] = []
        cursor += timedelta(days=1)

    for t in closed:
        if t.closed_at is None:
            continue
        key = t.closed_at.astimezone(UTC).date().isoformat()
        buckets.setdefault(key, []).append(t)

    out: list[DayStats] = []
    for day in sorted(buckets):
        rows = buckets[day]
        wins = sum(1 for t in rows if (t.pnl_r or 0.0) > 0)
        out.append(
            DayStats(
                day=day,
                trades=len(rows),
                wins=wins,
                losses=len(rows) - wins,
                net_pnl_usd=sum(float(t.pnl_usd or 0.0) for t in rows),
                expectancy_r=_mean([float(t.pnl_r or 0.0) for t in rows]),
            )
        )
    return out


def _win_rate_by_hour(closed: list[TradeRecord]) -> dict[str, float]:
    """Win rate bucketed by the UTC hour the position was opened."""
    buckets: dict[int, list[bool]] = {}
    for t in closed:
        hour = t.opened_at.astimezone(UTC).hour
        buckets.setdefault(hour, []).append((t.pnl_r or 0.0) > 0)
    return {
        f"{hour:02d}": sum(wins) / len(wins)
        for hour, wins in sorted(buckets.items())
        if wins
    }
