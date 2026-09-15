"""Build the AccountState the RiskEngine reads.

The previous implementation kept these counters in memory and only ever
updated the equity high-water mark, which meant the daily-loss, weekly-loss
and consecutive-loss gates could not fire at all: their inputs were
permanently zero. Everything here is therefore **derived from the trades
table on every tick**. A restart, a crash, or a second process all see the
same numbers, because the numbers are not state anyone is maintaining — they
are a query.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from app.db import system_state as ss
from app.db import trades as trades_db
from app.db.trades import TradeRecord, TradeStatus
from app.market.bars import bars_between
from pinned.risk_limits import RiskLimits

from .engine import is_weekend
from .types import AccountState, ExitReason, Side


def _day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start, start + timedelta(days=1)


def _week_bounds(now: datetime) -> tuple[datetime, datetime]:
    moment = now.astimezone(UTC)
    start_of_day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start_of_day - timedelta(days=moment.weekday())
    return start, start + timedelta(days=7)


def consecutive_losses(closed: list[TradeRecord]) -> int:
    """Count losses at the end of the sequence. `closed` is oldest-first."""
    n = 0
    for t in reversed(closed):
        if t.is_loss:
            n += 1
        else:
            break
    return n


def build_from_db(
    conn: sqlite3.Connection,
    *,
    equity: float,
    open_positions: int,
    now: datetime,
    limits: RiskLimits,
    granularity: str,
) -> AccountState:
    """Assemble the full AccountState from persisted rows."""
    now = now.astimezone(UTC)
    day_start, day_end = _day_bounds(now)
    week_start, week_end = _week_bounds(now)

    day_anchor, week_anchor = ss.ensure_anchors(conn, equity=equity, now=now)
    equity_high = ss.bump_equity_high(conn, equity)

    day_pnl = trades_db.realized_pnl_between(conn, day_start, day_end)
    week_pnl = trades_db.realized_pnl_between(conn, week_start, week_end)

    today_closed = trades_db.list_closed(conn, since=day_start, until=day_end, limit=500)
    today_opened = trades_db.opened_on_day(conn, day_start.date())
    losses_today = sum(1 for t in today_closed if t.is_loss)

    recent_closed = trades_db.list_closed(conn, limit=50)
    recent_oldest_first = list(reversed(recent_closed))

    last_closed = recent_closed[0] if recent_closed else None
    bars_since_last_exit: int | None = None
    if last_closed is not None and last_closed.closed_at is not None:
        bars_since_last_exit = bars_between(last_closed.closed_at, now, granularity)

    all_recent = trades_db.list_recent(conn, limit=20)
    last_entry = all_recent[0] if all_recent else None
    bars_since_last_entry: int | None = None
    if last_entry is not None:
        bars_since_last_entry = bars_between(last_entry.opened_at, now, granularity)

    kill = ss.load_kill_switch(conn)

    drawdown = (equity_high - equity) / equity_high if equity_high > 0 else 0.0

    return AccountState(
        equity=equity,
        equity_high=equity_high,
        day_start_equity=day_anchor.start_equity,
        week_start_equity=week_anchor.start_equity,
        day_pnl=day_pnl,
        week_pnl=week_pnl,
        drawdown_pct=max(0.0, drawdown),
        open_positions=open_positions,
        trades_today=len(today_opened),
        losses_today=losses_today,
        consecutive_losses=consecutive_losses(recent_oldest_first),
        bars_since_last_entry=bars_since_last_entry,
        bars_since_last_exit=bars_since_last_exit,
        last_exit_reason=last_closed.exit_reason if last_closed else None,
        last_exit_was_loss=bool(last_closed.is_loss) if last_closed else False,
        last_exit_side=last_closed.side if last_closed else None,
        last_trade_close_ts=last_closed.closed_at if last_closed else None,
        is_weekend=is_weekend(now, limits),
        is_kill_switch=kill.engaged,
        kill_switch_reason=kill.reason,
    )


def empty_state(equity: float, *, equity_high: float | None = None) -> AccountState:
    """A clean state, used by the backtester's first bar and by tests."""
    high = equity_high if equity_high is not None else equity
    return AccountState(
        equity=equity,
        equity_high=high,
        day_start_equity=equity,
        week_start_equity=equity,
        drawdown_pct=(high - equity) / high if high > 0 else 0.0,
    )


def update_after_close(
    state: AccountState,
    *,
    realized_pnl_usd: float,
    close_ts: datetime,
    side: Side,
    exit_reason: ExitReason,
    bars_held: int,
) -> AccountState:
    """Return a new AccountState with one trade closed.

    Pure; used by the backtester, whose whole world is in memory. The live
    loop does not call this — it re-derives from the database instead, so
    that a crash cannot lose a loss.
    """
    is_loss = realized_pnl_usd < 0
    new_equity = state.equity + realized_pnl_usd
    new_high = max(state.equity_high, new_equity)
    close = close_ts if close_ts.tzinfo else close_ts.replace(tzinfo=UTC)
    return state.model_copy(
        update={
            "equity": new_equity,
            "equity_high": new_high,
            "day_pnl": state.day_pnl + realized_pnl_usd,
            "week_pnl": state.week_pnl + realized_pnl_usd,
            "drawdown_pct": (new_high - new_equity) / new_high if new_high > 0 else 0.0,
            "open_positions": 0,
            "losses_today": state.losses_today + (1 if is_loss else 0),
            "consecutive_losses": state.consecutive_losses + 1 if is_loss else 0,
            "bars_since_last_exit": 0,
            "last_exit_reason": exit_reason,
            "last_exit_was_loss": is_loss,
            "last_exit_side": side,
            "last_trade_close_ts": close,
        }
    )


def update_after_open(state: AccountState, *, bars_since_last_entry: int = 0) -> AccountState:
    """Return a new AccountState with one position opened."""
    return state.model_copy(
        update={
            "open_positions": 1,
            "trades_today": state.trades_today + 1,
            "bars_since_last_entry": bars_since_last_entry,
        }
    )


def roll_day(state: AccountState) -> AccountState:
    """Reset the per-day counters at the UTC boundary."""
    return state.model_copy(
        update={
            "day_start_equity": state.equity,
            "day_pnl": 0.0,
            "trades_today": 0,
            "losses_today": 0,
        }
    )


def roll_week(state: AccountState) -> AccountState:
    return state.model_copy(
        update={"week_start_equity": state.equity, "week_pnl": 0.0}
    )


def open_position_count(conn: sqlite3.Connection) -> int:
    return sum(1 for t in trades_db.open_trades(conn) if t.status is not TradeStatus.CLOSED)
