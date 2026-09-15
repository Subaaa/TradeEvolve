"""The kill switch: what trips it, and who is allowed to reset it.

The old implementation had no automatic trigger at all — `engage()` had no
callers — so the only way to stop a losing run was a human editing the
database. Each trigger now has a test, and so does the rule that a weekly-loss
or drawdown switch needs a person to clear it.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from app.risk import kill_switch
from app.risk.account_state import empty_state
from app.risk.types import AccountState, ReasonCode
from pinned.risk_limits import RISK_LIMITS

pytestmark = pytest.mark.unit

NOW = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)
EQUITY = 10_000.0


def state(**overrides: object) -> AccountState:
    return empty_state(EQUITY).model_copy(update=overrides)


# ------------------------------------------------------------------ triggers


def test_a_clean_state_trips_nothing() -> None:
    assert kill_switch.check_auto_kill(state(), RISK_LIMITS) is None


def test_the_daily_loss_limit_trips_it() -> None:
    decision = kill_switch.check_auto_kill(
        state(day_pnl=-RISK_LIMITS.max_daily_loss_pct * EQUITY - 1), RISK_LIMITS
    )
    assert decision is not None
    assert decision.reason_code is ReasonCode.RISK_EXCEEDS_DAILY_LIMIT
    # A bad day should end the day, not the account.
    assert decision.requires_manual_clear is False


def test_hitting_the_daily_loss_count_trips_it() -> None:
    decision = kill_switch.check_auto_kill(
        state(losses_today=RISK_LIMITS.max_losses_per_day), RISK_LIMITS
    )
    assert decision is not None
    assert decision.reason_code is ReasonCode.MAX_LOSSES_PER_DAY
    assert decision.requires_manual_clear is False


def test_the_weekly_loss_limit_needs_a_human() -> None:
    decision = kill_switch.check_auto_kill(
        state(week_pnl=-RISK_LIMITS.max_weekly_loss_pct * EQUITY - 1), RISK_LIMITS
    )
    assert decision is not None
    assert decision.reason_code is ReasonCode.RISK_EXCEEDS_WEEKLY_LIMIT
    assert decision.requires_manual_clear is True


def test_the_drawdown_limit_needs_a_human() -> None:
    decision = kill_switch.check_auto_kill(
        state(drawdown_pct=RISK_LIMITS.max_drawdown_pct), RISK_LIMITS
    )
    assert decision is not None
    assert decision.reason_code is ReasonCode.RISK_EXCEEDS_MAX_DRAWDOWN
    assert decision.requires_manual_clear is True


def test_the_most_serious_breach_wins() -> None:
    """A week bad enough to trip both must engage the one needing a human."""
    decision = kill_switch.check_auto_kill(
        state(
            day_pnl=-1_000.0,
            week_pnl=-1_000.0,
            drawdown_pct=RISK_LIMITS.max_drawdown_pct,
        ),
        RISK_LIMITS,
    )
    assert decision is not None
    assert decision.requires_manual_clear is True


# ------------------------------------------------------------------- lifecycle


def test_engage_and_clear(conn: sqlite3.Connection) -> None:
    assert kill_switch.is_engaged(conn) is False
    kill_switch.engage(conn, reason="test", requires_manual_clear=False, now=NOW)
    assert kill_switch.is_engaged(conn) is True
    kill_switch.clear(conn)
    assert kill_switch.is_engaged(conn) is False


def test_an_automatic_clear_cannot_lift_a_manual_switch(
    conn: sqlite3.Connection,
) -> None:
    kill_switch.engage(conn, reason="drawdown", requires_manual_clear=True, now=NOW)
    kill_switch.clear(conn)  # the rollover job, without force
    assert kill_switch.is_engaged(conn) is True
    kill_switch.clear(conn, force=True)  # the CLI, with a person behind it
    assert kill_switch.is_engaged(conn) is False


def test_a_daily_switch_clears_at_the_rollover(conn: sqlite3.Connection) -> None:
    kill_switch.engage(conn, reason="daily loss", requires_manual_clear=False, now=NOW)
    assert kill_switch.clear_if_daily(conn) is True
    assert kill_switch.is_engaged(conn) is False


def test_a_manual_switch_survives_the_rollover(conn: sqlite3.Connection) -> None:
    kill_switch.engage(conn, reason="weekly loss", requires_manual_clear=True, now=NOW)
    assert kill_switch.clear_if_daily(conn) is False
    assert kill_switch.is_engaged(conn) is True


def test_a_daily_switch_never_downgrades_a_manual_one(
    conn: sqlite3.Connection,
) -> None:
    kill_switch.engage(conn, reason="drawdown", requires_manual_clear=True, now=NOW)
    kill_switch.engage(conn, reason="daily loss", requires_manual_clear=False, now=NOW)
    current = kill_switch.check_auto_kill(state(), RISK_LIMITS)
    assert current is None
    from app.db import system_state as ss

    assert ss.load_kill_switch(conn).requires_manual_clear is True
