"""The deterministic RiskEngine.

`evaluate(proposal, state, limits, market_state, now)` is a **pure function**.
No network, no I/O, no LLM. It is the only component that turns a `Proposal`
into an `OrderSpec`.

The order of checks is deliberate and is itself a safety property:

1.  **Hard brakes** (kill switch, daily, weekly, drawdown) come first, so a
    malformed proposal can never be rejected for a shape reason in a way that
    hides the fact that a loss limit had already been breached.
2.  **Discipline gates** come second. These are the rules that exist because
    humans break them: trade count, loss count, cooldowns after losses,
    minimum spacing, one position at a time.
3.  **Shape gates** come last: stop present, stop far enough to pay costs,
    stop not absurdly wide, R/R, per-trade risk, margin, unit cap.

The same function runs in the backtester, so a rule that would have blocked a
live trade also blocks it in every historical evaluation. That equivalence is
the whole reason the backtest is worth anything.

If you find yourself wanting to add an LLM call here, you are wrong.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pinned.risk_limits import RiskLimits

from .sizing import margin_required
from .types import (
    AccountState,
    Decision,
    ExitReason,
    MarketState,
    OrderSpec,
    Proposal,
    ReasonCode,
    Side,
    TimeInForce,
)


def _reject(code: ReasonCode, notes: str) -> Decision:
    return Decision(decision="rejected", reason_codes=[code], notes=notes)


def _skip(code: ReasonCode, notes: str) -> Decision:
    return Decision(decision="skipped", reason_codes=[code], notes=notes)


def is_weekend(now: datetime, limits: RiskLimits) -> bool:
    """True inside the spot-gold weekend window.

    PAXG trades continuously, but it tracks a market that does not: weekend
    liquidity is thin and the spread widens, which turns a 20bps stop into a
    coin flip. We simply do not enter.
    """
    if not limits.no_entries_on_weekend:
        return False
    moment = now.astimezone(UTC)
    weekday = moment.weekday()
    hour = moment.hour
    start_day = limits.weekend_start_weekday
    end_day = limits.weekend_end_weekday
    if weekday == start_day:
        return hour >= limits.weekend_start_hour_utc
    if weekday == end_day:
        return hour < limits.weekend_end_hour_utc
    return weekday > start_day and weekday < end_day


def evaluate(
    proposal: Proposal,
    state: AccountState,
    limits: RiskLimits,
    market_state: MarketState,
    now: datetime,
) -> Decision:
    """Validate a Proposal under the given state and limits."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    # ---------------------------------------------------------- hard brakes

    if state.is_kill_switch:
        return _reject(
            ReasonCode.KILL_SWITCH_ENGAGED,
            f"kill switch engaged: {state.kill_switch_reason or 'unspecified'}",
        )

    # Daily loss is measured against start-of-day equity, never against the
    # high-water mark: a winning morning must not enlarge the afternoon's
    # permitted loss.
    if state.day_start_equity > 0:
        max_day_loss = limits.max_daily_loss_pct * state.day_start_equity
        if state.day_pnl <= -max_day_loss:
            return _skip(
                ReasonCode.RISK_EXCEEDS_DAILY_LIMIT,
                "daily loss limit breached; no new positions until the next UTC day",
            )

    if state.week_start_equity > 0:
        max_week_loss = limits.max_weekly_loss_pct * state.week_start_equity
        if state.week_pnl <= -max_week_loss:
            return _reject(
                ReasonCode.RISK_EXCEEDS_WEEKLY_LIMIT,
                "weekly loss limit breached; kill switch requires a manual clear",
            )

    if state.drawdown_pct >= limits.max_drawdown_pct:
        return _reject(
            ReasonCode.RISK_EXCEEDS_MAX_DRAWDOWN,
            "max drawdown reached; kill switch requires a manual clear",
        )

    # ------------------------------------------------------------ discipline

    if state.is_weekend:
        return _skip(ReasonCode.MARKET_CLOSED, "weekend: no new entries")

    if state.trades_today >= limits.max_trades_per_day:
        return _skip(
            ReasonCode.MAX_TRADES_PER_DAY,
            f"{state.trades_today} trades already taken today",
        )

    if state.losses_today >= limits.max_losses_per_day:
        return _skip(
            ReasonCode.MAX_LOSSES_PER_DAY,
            f"{state.losses_today} losses today; the day is over",
        )

    if (
        state.consecutive_losses >= limits.cooldown_after_consecutive_losses
        and state.bars_since_last_exit is not None
        and state.bars_since_last_exit < limits.cooldown_after_consecutive_losses_bars
    ):
        return _skip(
            ReasonCode.CONSECUTIVE_LOSS_COOLDOWN,
            f"{state.consecutive_losses} consecutive losses; cooling down",
        )

    if (
        state.last_exit_reason in (ExitReason.SL, ExitReason.SL_FAILSAFE)
        and state.bars_since_last_exit is not None
        and state.bars_since_last_exit < limits.cooldown_after_stop_out_bars
    ):
        return _skip(
            ReasonCode.STOP_OUT_COOLDOWN,
            "stopped out recently; no immediate re-entry",
        )

    if (
        state.last_exit_was_loss
        and state.last_exit_side == proposal.side
        and state.bars_since_last_exit is not None
        and state.bars_since_last_exit < limits.reentry_after_loss_same_direction_bars
    ):
        return _skip(
            ReasonCode.REENTRY_AFTER_LOSS_COOLDOWN,
            "last trade in this direction lost; not re-entering the same way yet",
        )

    if (
        state.bars_since_last_entry is not None
        and state.bars_since_last_entry < limits.min_bars_between_entries
    ):
        return _skip(
            ReasonCode.MIN_BARS_BETWEEN_ENTRIES,
            "too soon after the previous entry",
        )

    if state.open_positions >= limits.max_open_positions:
        return _skip(
            ReasonCode.MAX_POSITIONS_REACHED, "max open positions already reached"
        )

    # Belt and braces with the check above: two independent gates must both
    # fail before a second position can ever be opened.
    if limits.no_add_to_position and state.open_positions > 0:
        return _skip(
            ReasonCode.NO_ADD_TO_POSITION,
            "a position is already open; never scale in, never average down",
        )

    if not limits.allow_short and proposal.side == Side.SHORT:
        return _reject(
            ReasonCode.SHORT_NOT_SUPPORTED,
            "shorting is disabled (Alpaca crypto cannot be sold short)",
        )

    # ------------------------------------------------------------------ data

    if market_state.seconds_since_last_bar > limits.stale_data_max_age_sec:
        return _skip(
            ReasonCode.STALE_DATA,
            f"market data is {market_state.seconds_since_last_bar}s old",
        )

    # ------------------------------------------------------------ trade shape

    reasons: list[ReasonCode] = []

    if limits.mandatory_stop_loss and proposal.stop_loss is None:
        # Nothing below can be meaningfully evaluated without a stop.
        return _reject(
            ReasonCode.MANDATORY_STOP_LOSS_MISSING, "proposal has no stop loss"
        )
    if proposal.take_profit is None:
        return _reject(ReasonCode.INVALID_PROPOSAL, "proposal has no take profit")

    stop_loss = proposal.stop_loss
    assert stop_loss is not None  # narrowed by the mandatory-SL gate above
    sl_distance = abs(proposal.entry_price - stop_loss)
    if sl_distance <= 0.0 or proposal.entry_price <= 0.0:
        return _reject(ReasonCode.INVALID_PROPOSAL, "degenerate entry/stop prices")

    sl_bps = sl_distance / proposal.entry_price * 10_000.0
    if sl_bps < limits.min_sl_distance_bps:
        reasons.append(ReasonCode.SL_TOO_TIGHT_FOR_COSTS)

    atr_value = market_state.indicators.get("atr") or proposal.indicators.get("atr")
    if atr_value and atr_value > 0 and sl_distance > limits.max_sl_distance_atr_multiple * atr_value:
        reasons.append(ReasonCode.SL_OUT_OF_RANGE)

    if proposal.expected_rr < limits.min_rr_ratio:
        reasons.append(ReasonCode.MIN_RR_BELOW_FLOOR)

    if proposal.expected_risk_usd > limits.max_risk_per_trade_pct * state.equity:
        reasons.append(ReasonCode.RISK_EXCEEDS_PER_TRADE)

    # Notional may legitimately exceed equity under leverage; what must stay
    # bounded is the margin the position ties up.
    if margin_required(proposal.units, proposal.entry_price) > (
        limits.max_margin_per_position_pct * state.equity
    ):
        reasons.append(ReasonCode.RISK_EXCEEDS_POSITION_VALUE)

    if proposal.units > limits.max_units:
        reasons.append(ReasonCode.RISK_EXCEEDS_MAX_UNITS)

    if reasons:
        return Decision(
            decision="rejected",
            reason_codes=reasons,
            notes="risk engine did not approve the proposal's shape",
        )

    order = OrderSpec(
        instrument=proposal.instrument,
        side=proposal.side,
        order_type=proposal.order_type,
        units=proposal.units,
        entry_price=proposal.entry_price,
        stop_loss=stop_loss,
        take_profit=proposal.take_profit,
        tif=TimeInForce.IOC,
        client_tag=proposal.client_tag,
        expected_rr=proposal.expected_rr,
        bar_ts=proposal.bar_ts,
        strategy_version_id=proposal.strategy_version_id,
        indicators=dict(proposal.indicators),
    )
    return Decision(
        decision="approved",
        reason_codes=[ReasonCode.OK],
        order=order,
        notes="risk engine approved",
    )
