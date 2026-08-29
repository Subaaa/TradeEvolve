"""Account state helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from app.risk.account_state import empty_state, update_after_close
from app.risk.types import AccountState


def test_empty_state_drawdown_zero() -> None:
    s = empty_state(equity=10_000.0, equity_high=10_000.0)
    assert s.drawdown_pct == 0.0
    assert s.consecutive_losses == 0


def test_update_after_close_loss_increments_consecutive() -> None:
    s = empty_state(10_000.0, 10_000.0)
    s2 = update_after_close(
        s,
        realized_pnl_usd=-50.0,
        close_ts=datetime(2025, 1, 1, 12, tzinfo=timezone.utc),
    )
    assert s2.equity == 9_950.0
    assert s2.consecutive_losses == 1
    assert s2.equity_high == 10_000.0
    assert s2.drawdown_pct > 0


def test_update_after_close_win_resets_consecutive() -> None:
    s = empty_state(10_000.0, 10_000.0)
    s = update_after_close(s, realized_pnl_usd=-50.0, close_ts=datetime(2025, 1, 1, 12, tzinfo=timezone.utc))
    s2 = update_after_close(s, realized_pnl_usd=20.0, close_ts=datetime(2025, 1, 2, 12, tzinfo=timezone.utc))
    assert s2.consecutive_losses == 0
    assert s2.equity_high == 10_000.0  # new equity < high


def test_update_preserves_equity_high() -> None:
    s = empty_state(10_000.0, 10_000.0)
    s2 = update_after_close(s, realized_pnl_usd=500.0, close_ts=datetime(2025, 1, 1, 12, tzinfo=timezone.utc))
    assert s2.equity_high == 10_500.0
    assert s2.drawdown_pct == 0.0
