"""The deterministic RiskEngine.

`evaluate(proposal, state, limits, news_policy, market_state, now)` is a
**pure function**. No network, no I/O, no LLM. The function is the only
component that decides whether a `Proposal` becomes an `OrderSpec`.

The order of checks is intentional: the cheapest checks first, and
the most-critical safety checks (kill switch, daily loss) before
shape checks (R/R, stop-loss) so that a shape failure is not used to
bypass a risk limit.

If you find yourself wanting to add an LLM call here, you are wrong.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pinned.news_policy import NewsPolicy
from pinned.risk_limits import RiskLimits

from .types import (
    AccountState,
    Decision,
    MarketState,
    OrderSpec,
    OrderType,
    Proposal,
    ReasonCode,
    Side,
    TimeInForce,
)


def evaluate(
    proposal: Proposal,
    state: AccountState,
    limits: RiskLimits,
    news_policy: NewsPolicy,
    market_state: MarketState,
    now: datetime,
) -> Decision:
    """Validate a Proposal under the given state and limits.

    Returns a `Decision` with `decision="approved"` and a populated
    `order`, or `decision="rejected" | "skipped"` with `reason_codes`.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    reasons: list[ReasonCode] = []
    skipped = False

    # 1. Kill switch (always wins)
    if state.is_kill_switch:
        return Decision(
            decision="rejected",
            reason_codes=[ReasonCode.KILL_SWITCH_ENGAGED],
            notes="kill switch is engaged",
        )

    # 2. Daily/weekly loss limits
    max_day_loss = limits.max_daily_loss_pct * (state.equity_high)
    if state.day_pnl <= -max_day_loss:
        return Decision(
            decision="skipped",
            reason_codes=[ReasonCode.RISK_EXCEEDS_DAILY_LIMIT],
            notes="daily loss limit breached; no new positions until next UTC day",
        )

    max_week_loss = limits.max_weekly_loss_pct * state.equity_high
    if state.week_pnl <= -max_week_loss:
        return Decision(
            decision="rejected",
            reason_codes=[ReasonCode.RISK_EXCEEDS_WEEKLY_LIMIT],
            notes="weekly loss limit breached; kill switch would have engaged",
        )

    # 3. Max drawdown
    if state.drawdown_pct >= limits.max_drawdown_pct:
        return Decision(
            decision="rejected",
            reason_codes=[ReasonCode.RISK_EXCEEDS_MAX_DRAWDOWN],
            notes="max drawdown reached",
        )

    # 4. Stale data
    if market_state.seconds_since_last_bar > limits.stale_data_max_age_sec:
        reasons.append(ReasonCode.STALE_DATA)
        skipped = True

    # 5. News policy
    if news_policy == NewsPolicy.NO_NEW_POSITIONS:
        return Decision(
            decision="skipped",
            reason_codes=[ReasonCode.NEWS_NO_NEW_POSITIONS],
            notes="news policy: no new positions",
        )
    if news_policy == NewsPolicy.REDUCE_SIZE:
        # Not a hard block; flagged below in the size cap.
        pass

    # 6. Cooldown after consecutive losses
    if (
        state.consecutive_losses >= limits.cooldown_after_consecutive_losses
        and state.last_trade_close_ts is not None
        and now - state.last_trade_close_ts
        < timedelta(hours=limits.cooldown_after_consecutive_loss_hours)
    ):
        return Decision(
            decision="skipped",
            reason_codes=[ReasonCode.CONSECUTIVE_LOSS_COOLDOWN],
            notes="cooldown after consecutive losses",
        )

    # 7. Max open positions
    if state.open_positions >= limits.max_open_positions:
        return Decision(
            decision="skipped",
            reason_codes=[ReasonCode.MAX_POSITIONS_REACHED],
            notes="max open positions already reached",
        )

    # 8. Mandatory stop-loss
    if limits.mandatory_stop_loss and proposal.stop_loss is None:
        reasons.append(ReasonCode.MANDATORY_STOP_LOSS_MISSING)

    # 9. R/R floor
    if proposal.expected_rr < limits.min_rr_ratio:
        reasons.append(ReasonCode.MIN_RR_BELOW_FLOOR)

    # 10. SL within ±3 ATR — we approximate by checking the SL distance vs the
    #     expected_risk_usd. A tighter test (using the strategy's ATR) lives
    #     in the StrategyRunner; the RiskEngine only enforces a wide ceiling.
    if proposal.stop_loss is not None:
        sl_distance = abs(proposal.entry_price - proposal.stop_loss)
        # We use the most-recent ATR from the market state if provided
        atr_value = market_state.indicators.get("atr_14")
        if atr_value and atr_value > 0:
            if sl_distance > limits.max_sl_distance_atr_multiple * atr_value:
                reasons.append(ReasonCode.SL_OUT_OF_RANGE)

    # 11. Per-trade risk cap
    if proposal.expected_risk_usd > limits.max_risk_per_trade_pct * state.equity:
        reasons.append(ReasonCode.RISK_EXCEEDS_PER_TRADE)

    # 12. Position value cap
    position_value = abs(proposal.units * proposal.entry_price)
    if position_value > limits.max_position_value_pct * state.equity:
        reasons.append(ReasonCode.RISK_EXCEEDS_POSITION_VALUE)

    # 13. Max units
    if proposal.units > limits.max_units:
        reasons.append(ReasonCode.RISK_EXCEEDS_MAX_UNITS)

    # Final: if any reasons, reject; if no reasons, approve.
    if reasons:
        return Decision(
            decision="skipped" if skipped else "rejected",
            reason_codes=reasons,
            notes="risk engine did not approve",
        )

    # Build the OrderSpec, applying the size-reduction factor for news policy.
    units = proposal.units
    if news_policy == NewsPolicy.REDUCE_SIZE:
        from pinned.news_policy import REDUCE_SIZE_FACTOR
        units = max(1, int(units * REDUCE_SIZE_FACTOR))

    order = OrderSpec(
        instrument=proposal.instrument,
        side=proposal.side,
        order_type=proposal.order_type,
        units=units,
        entry_price=proposal.entry_price,
        stop_loss=proposal.stop_loss,
        take_profit=proposal.take_profit,
        tif=TimeInForce.GTC,
        client_tag=proposal.client_tag,
        expected_rr=proposal.expected_rr,
    )
    return Decision(
        decision="approved",
        reason_codes=[ReasonCode.OK],
        order=order,
        notes="risk engine approved",
    )
