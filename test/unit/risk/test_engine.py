"""Property-based and unit tests for the deterministic RiskEngine.

These tests are the gate that decides whether a code change may ship.
They assert: no Proposal can ever be approved if it breaches any limit.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.risk.engine import evaluate
from app.risk.types import (
    AccountState,
    MarketState,
    OrderType,
    Proposal,
    ReasonCode,
    Side,
)
from app.risk.sizing import floor_to_lot, margin_required
from pinned.news_policy import NewsPolicy
from pinned.risk_limits import RISK_LIMITS, RiskLimits
from pinned.simulation_constants import SIM


# ---------- Helpers ---------------------------------------------------------


def _base_proposal(
    *,
    units: float = 1.0,
    entry: float = 2000.0,
    sl: float | None = 1990.0,
    tp: float | None = 2030.0,
    rr: float = 3.0,
    risk_usd: float = 5.0,
) -> Proposal:
    return Proposal(
        proposal_id="p-1",
        strategy_version_id=1,
        instrument="XAU_USD",
        side=Side.LONG,
        order_type=OrderType.MARKET,
        units=units,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        expected_rr=rr,
        expected_risk_usd=risk_usd,
        bar_ts=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
        client_tag="abc",
    )


def _base_state(**overrides) -> AccountState:
    base = dict(
        equity=10_000.0,
        equity_high=10_000.0,
        day_pnl=0.0,
        week_pnl=0.0,
        drawdown_pct=0.0,
        open_positions=0,
        consecutive_losses=0,
        is_kill_switch=False,
    )
    base.update(overrides)
    return AccountState(**base)


def _base_market(**overrides) -> MarketState:
    base = dict(
        instrument="XAU_USD",
        granularity="H1",
        last_bar_ts=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
        last_close=2000.0,
        indicators={"atr_14": 10.0},
        seconds_since_last_bar=0,
    )
    base.update(overrides)
    return MarketState(**base)


def _now() -> datetime:
    return datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)


# ---------- Basic approval --------------------------------------------------


def test_approve_clean_proposal() -> None:
    p = _base_proposal()
    d = evaluate(p, _base_state(), RISK_LIMITS, NewsPolicy.NORMAL, _base_market(), _now())
    assert d.decision == "approved"
    assert d.reason_codes == [ReasonCode.OK]
    assert d.order is not None
    assert d.order.units == p.units


def test_reduce_size_factor_applied() -> None:
    from pinned.news_policy import REDUCE_SIZE_FACTOR

    p = _base_proposal(units=1.0)
    d = evaluate(p, _base_state(), RISK_LIMITS, NewsPolicy.REDUCE_SIZE, _base_market(), _now())
    assert d.decision == "approved"
    assert d.order is not None
    assert d.order.units == floor_to_lot(1.0 * REDUCE_SIZE_FACTOR, SIM.min_lot_step)
    assert d.order.units < p.units


def test_reduce_size_below_one_lot_step_is_skipped() -> None:
    p = _base_proposal(units=SIM.min_lot_step)
    d = evaluate(p, _base_state(), RISK_LIMITS, NewsPolicy.REDUCE_SIZE, _base_market(), _now())
    assert d.decision == "skipped"
    assert d.order is None


# ---------- Kill switch -----------------------------------------------------


def test_kill_switch_blocks_everything() -> None:
    p = _base_proposal()
    d = evaluate(
        p,
        _base_state(is_kill_switch=True),
        RISK_LIMITS,
        NewsPolicy.NORMAL,
        _base_market(),
        _now(),
    )
    assert d.decision == "rejected"
    assert d.reason_codes == [ReasonCode.KILL_SWITCH_ENGAGED]


# ---------- Daily / weekly loss --------------------------------------------


def test_daily_loss_breach_skips() -> None:
    p = _base_proposal()
    d = evaluate(
        p,
        _base_state(day_pnl=-400.0),  # -4% of 10k; limit is 3%
        RISK_LIMITS,
        NewsPolicy.NORMAL,
        _base_market(),
        _now(),
    )
    assert d.decision == "skipped"
    assert d.reason_codes == [ReasonCode.RISK_EXCEEDS_DAILY_LIMIT]


def test_weekly_loss_breach_rejects() -> None:
    p = _base_proposal()
    d = evaluate(
        p,
        _base_state(week_pnl=-700.0),  # -7% of 10k; limit is 6%
        RISK_LIMITS,
        NewsPolicy.NORMAL,
        _base_market(),
        _now(),
    )
    assert d.decision == "rejected"
    assert ReasonCode.RISK_EXCEEDS_WEEKLY_LIMIT in d.reason_codes


# ---------- Drawdown --------------------------------------------------------


def test_drawdown_breach_rejects() -> None:
    p = _base_proposal()
    d = evaluate(
        p,
        _base_state(drawdown_pct=0.20),  # limit is 0.15
        RISK_LIMITS,
        NewsPolicy.NORMAL,
        _base_market(),
        _now(),
    )
    assert d.decision == "rejected"
    assert ReasonCode.RISK_EXCEEDS_MAX_DRAWDOWN in d.reason_codes


# ---------- Per-trade risk and position value ------------------------------


def test_per_trade_risk_breach_rejects() -> None:
    p = _base_proposal(risk_usd=100.0)  # limit 0.5% of 10k = 50
    d = evaluate(p, _base_state(), RISK_LIMITS, NewsPolicy.NORMAL, _base_market(), _now())
    assert d.decision == "rejected"
    assert ReasonCode.RISK_EXCEEDS_PER_TRADE in d.reason_codes


def test_margin_breach_rejects() -> None:
    # 200 oz @ 2000 = 400k notional; at leverage 1.0 that is 400k of margin
    # against 10k equity, far over the 20% cap.
    p = _base_proposal(units=200, entry=2000.0)
    d = evaluate(p, _base_state(), RISK_LIMITS, NewsPolicy.NORMAL, _base_market(), _now())
    assert d.decision == "rejected"
    assert ReasonCode.RISK_EXCEEDS_POSITION_VALUE in d.reason_codes


def test_max_units_breach_rejects() -> None:
    p = _base_proposal(units=200)
    d = evaluate(p, _base_state(), RISK_LIMITS, NewsPolicy.NORMAL, _base_market(), _now())
    assert d.decision == "rejected"
    assert ReasonCode.RISK_EXCEEDS_MAX_UNITS in d.reason_codes


# ---------- R/R and stop-loss ----------------------------------------------


def test_low_rr_rejects() -> None:
    p = _base_proposal(rr=1.0)  # floor is 1.5
    d = evaluate(p, _base_state(), RISK_LIMITS, NewsPolicy.NORMAL, _base_market(), _now())
    assert d.decision == "rejected"
    assert ReasonCode.MIN_RR_BELOW_FLOOR in d.reason_codes


def test_missing_sl_rejects() -> None:
    p = _base_proposal(sl=None)
    d = evaluate(p, _base_state(), RISK_LIMITS, NewsPolicy.NORMAL, _base_market(), _now())
    assert d.decision == "rejected"
    assert ReasonCode.MANDATORY_STOP_LOSS_MISSING in d.reason_codes


def test_sl_too_far_away_rejects() -> None:
    # ATR is 10; SL_OUT_OF_RANGE if sl distance > 3*10 = 30
    p = _base_proposal(sl=1900.0)  # distance 100
    d = evaluate(p, _base_state(), RISK_LIMITS, NewsPolicy.NORMAL, _base_market(), _now())
    assert d.decision == "rejected"
    assert ReasonCode.SL_OUT_OF_RANGE in d.reason_codes


# ---------- Stale data ------------------------------------------------------


def test_stale_data_skips() -> None:
    p = _base_proposal()
    d = evaluate(
        p,
        _base_state(),
        RISK_LIMITS,
        NewsPolicy.NORMAL,
        _base_market(seconds_since_last_bar=1000),  # limit 300
        _now(),
    )
    assert d.decision == "skipped"
    assert ReasonCode.STALE_DATA in d.reason_codes


# ---------- News policy -----------------------------------------------------


def test_no_new_positions_blocks() -> None:
    p = _base_proposal()
    d = evaluate(p, _base_state(), RISK_LIMITS, NewsPolicy.NO_NEW_POSITIONS, _base_market(), _now())
    assert d.decision == "skipped"
    assert d.reason_codes == [ReasonCode.NEWS_NO_NEW_POSITIONS]


# ---------- Cooldown --------------------------------------------------------


def test_consecutive_loss_cooldown() -> None:
    p = _base_proposal()
    state = _base_state(
        consecutive_losses=3,
        last_trade_close_ts=_now() - timedelta(hours=1),  # within 24h cooldown
    )
    d = evaluate(p, state, RISK_LIMITS, NewsPolicy.NORMAL, _base_market(), _now())
    assert d.decision == "skipped"
    assert d.reason_codes == [ReasonCode.CONSECUTIVE_LOSS_COOLDOWN]


def test_consecutive_loss_cooldown_expires() -> None:
    p = _base_proposal()
    state = _base_state(
        consecutive_losses=3,
        last_trade_close_ts=_now() - timedelta(hours=25),
    )
    d = evaluate(p, state, RISK_LIMITS, NewsPolicy.NORMAL, _base_market(), _now())
    assert d.decision == "approved"


# ---------- Max positions ---------------------------------------------------


def test_max_positions_skips() -> None:
    p = _base_proposal()
    d = evaluate(
        p,
        _base_state(open_positions=1),
        RISK_LIMITS,
        NewsPolicy.NORMAL,
        _base_market(),
        _now(),
    )
    assert d.decision == "skipped"
    assert d.reason_codes == [ReasonCode.MAX_POSITIONS_REACHED]


# ---------- Property-based: no breach can ever be approved -----------------


limits_st = st.builds(
    RiskLimits,
    max_risk_per_trade_pct=st.floats(min_value=0.001, max_value=0.05),
    max_daily_loss_pct=st.floats(min_value=0.01, max_value=0.10),
    max_weekly_loss_pct=st.floats(min_value=0.02, max_value=0.20),
    max_drawdown_pct=st.floats(min_value=0.05, max_value=0.30),
    min_rr_ratio=st.floats(min_value=1.0, max_value=3.0),
    max_units=st.floats(min_value=1.0, max_value=1000.0),
    max_margin_per_position_pct=st.floats(min_value=0.01, max_value=1.0),
)


@given(
    units=st.floats(min_value=0.001, max_value=2000.0, allow_nan=False),
    entry=st.floats(min_value=100.0, max_value=10000.0, allow_nan=False),
    rr=st.floats(min_value=0.0, max_value=10.0, allow_nan=False),
    risk_usd=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False),
    equity=st.floats(min_value=100.0, max_value=1_000_000.0, allow_nan=False),
    limits=limits_st,
)
@settings(max_examples=300, deadline=None)
def test_property_no_breach_ever_approved(
    units: float, entry: float, rr: float, risk_usd: float, equity: float, limits: RiskLimits
) -> None:
    p = _base_proposal(units=units, entry=entry, rr=rr, risk_usd=risk_usd, sl=entry - 1.0)
    state = _base_state(equity=equity, equity_high=equity)
    d = evaluate(p, state, limits, NewsPolicy.NORMAL, _base_market(), _now())

    # If the proposal is approved, it must satisfy every per-trade limit.
    if d.decision == "approved":
        assert d.order is not None
        # Per-trade risk
        if p.expected_risk_usd > limits.max_risk_per_trade_pct * equity:
            raise AssertionError("approved despite per-trade risk breach")
        # Margin
        if margin_required(units, entry) > limits.max_margin_per_position_pct * equity:
            raise AssertionError("approved despite margin breach")
        # Max units
        if units > limits.max_units:
            raise AssertionError("approved despite max units breach")
        # R/R
        if rr < limits.min_rr_ratio:
            raise AssertionError("approved despite R/R below floor")
