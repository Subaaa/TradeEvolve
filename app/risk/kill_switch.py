"""The kill switch: automatic engagement, and who is allowed to clear it.

The old implementation had `engage()` and `clear()` that nothing ever called,
talking to a protocol with no implementation, while the live loop read the
flag through a different API entirely. The switch could only be thrown by a
human editing the database by hand — which is to say, never, at 3am, during
the drawdown it existed to stop.

Three triggers, three different clearing rules:

* **daily loss** — auto-engages, auto-clears at the next UTC midnight. A bad
  day should end the day, not the account.
* **weekly loss** — auto-engages, requires a human to clear. A bad week is a
  signal about the strategy, not about the market.
* **max drawdown** — auto-engages, requires a human to clear.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.db import system_state as ss
from app.db.system_state import KillSwitch
from pinned.risk_limits import RISK_LIMITS, RiskLimits

from .types import AccountState, ReasonCode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KillDecision:
    reason_code: ReasonCode
    reason: str
    requires_manual_clear: bool
    value: float


def check_auto_kill(
    state: AccountState, limits: RiskLimits = RISK_LIMITS
) -> KillDecision | None:
    """Return the kill decision this state warrants, or None.

    Checked in severity order, so a week that is bad enough to trip both the
    daily and the weekly limit engages the one that needs a human.
    """
    if state.drawdown_pct >= limits.max_drawdown_pct:
        return KillDecision(
            reason_code=ReasonCode.RISK_EXCEEDS_MAX_DRAWDOWN,
            reason=(
                f"drawdown {state.drawdown_pct:.2%} reached the "
                f"{limits.max_drawdown_pct:.2%} ceiling"
            ),
            requires_manual_clear=limits.drawdown_requires_manual_clear,
            value=state.drawdown_pct,
        )

    if state.week_start_equity > 0:
        week_limit = limits.max_weekly_loss_pct * state.week_start_equity
        if state.week_pnl <= -week_limit:
            return KillDecision(
                reason_code=ReasonCode.RISK_EXCEEDS_WEEKLY_LIMIT,
                reason=(
                    f"weekly loss {state.week_pnl:.2f} USD breached the "
                    f"{limits.max_weekly_loss_pct:.2%} limit"
                ),
                requires_manual_clear=limits.weekly_loss_requires_manual_clear,
                value=state.week_pnl,
            )

    if state.day_start_equity > 0:
        day_limit = limits.max_daily_loss_pct * state.day_start_equity
        day_loss_hit = state.day_pnl <= -day_limit
        loss_count_hit = state.losses_today >= limits.max_losses_per_day
        if day_loss_hit or loss_count_hit:
            why = (
                f"daily loss {state.day_pnl:.2f} USD breached the "
                f"{limits.max_daily_loss_pct:.2%} limit"
                if day_loss_hit
                else f"{state.losses_today} losses today reached the daily cap"
            )
            return KillDecision(
                reason_code=(
                    ReasonCode.RISK_EXCEEDS_DAILY_LIMIT
                    if day_loss_hit
                    else ReasonCode.MAX_LOSSES_PER_DAY
                ),
                reason=why,
                # Auto-clears at the next UTC day.
                requires_manual_clear=not limits.daily_loss_engages_kill_switch_until_next_utc_day,
                value=state.day_pnl,
            )

    return None


def engage(
    conn: sqlite3.Connection,
    *,
    reason: str,
    requires_manual_clear: bool,
    now: datetime | None = None,
) -> KillSwitch:
    """Engage the switch. Never downgrades an existing manual-clear switch."""
    current = ss.load_kill_switch(conn)
    if current.engaged and current.requires_manual_clear:
        return current
    ks = KillSwitch(
        engaged=True,
        reason=reason,
        requires_manual_clear=requires_manual_clear,
        engaged_at=now or datetime.now(tz=UTC),
    )
    ss.save_kill_switch(conn, ks)
    logger.error("KILL SWITCH ENGAGED: %s (manual clear: %s)", reason, requires_manual_clear)
    return ks


def clear(conn: sqlite3.Connection, *, force: bool = False) -> KillSwitch:
    """Clear the switch.

    `force` is what the CLI passes: it is the human saying yes. Automatic
    callers (the daily rollover) leave it False, so they can only clear a
    switch that was auto-clearable in the first place.
    """
    current = ss.load_kill_switch(conn)
    if current.engaged and current.requires_manual_clear and not force:
        return current
    ks = KillSwitch()
    ss.save_kill_switch(conn, ks)
    if current.engaged:
        logger.warning("kill switch cleared (was: %s)", current.reason)
    return ks


def is_engaged(conn: sqlite3.Connection) -> bool:
    return ss.load_kill_switch(conn).engaged


def clear_if_daily(conn: sqlite3.Connection) -> bool:
    """Called at the UTC rollover. Returns True if it actually cleared."""
    current = ss.load_kill_switch(conn)
    if current.engaged and not current.requires_manual_clear:
        clear(conn)
        return True
    return False
