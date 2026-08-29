"""Builds an AccountState snapshot for the RiskEngine.

In the MVP the source of truth is the OANDA fxTrade Practice account;
in Phase 7 this would also be a live account (gated by `LIVE_TRADING_ENABLED`).

This module is intentionally small: it just queries the OANDA MCP
client and shapes the response.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .types import AccountState


def empty_state(equity: float, equity_high: float) -> AccountState:
    return AccountState(
        equity=equity,
        equity_high=equity_high,
        day_pnl=0.0,
        week_pnl=0.0,
        drawdown_pct=(equity_high - equity) / equity_high if equity_high > 0 else 0.0,
        open_positions=0,
        consecutive_losses=0,
        is_kill_switch=False,
    )


def update_after_close(
    state: AccountState,
    *,
    realized_pnl_usd: float,
    close_ts: datetime,
) -> AccountState:
    """Return a new AccountState with one trade closed.

    Pure: does not mutate `state`.
    """
    new_equity = state.equity + realized_pnl_usd
    new_equity_high = max(state.equity_high, new_equity)
    new_day_pnl = state.day_pnl + realized_pnl_usd
    new_week_pnl = state.week_pnl + realized_pnl_usd
    if realized_pnl_usd < 0:
        consecutive = state.consecutive_losses + 1
    elif realized_pnl_usd > 0:
        consecutive = 0
    else:
        consecutive = 0
    return AccountState(
        equity=new_equity,
        equity_high=new_equity_high,
        day_pnl=new_day_pnl,
        week_pnl=new_week_pnl,
        drawdown_pct=(new_equity_high - new_equity) / new_equity_high,
        open_positions=state.open_positions,
        last_n_trade_outcomes=state.last_n_trade_outcomes,
        consecutive_losses=consecutive,
        last_trade_close_ts=close_ts if close_ts.tzinfo else close_ts.replace(tzinfo=timezone.utc),
        is_kill_switch=state.is_kill_switch,
    )
