"""Every discipline gate in the risk engine.

These are the rules that exist because humans break them, so each one gets an
explicit test: if a gate is ever removed or reordered so that it stops firing,
a test fails rather than a position being opened.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.risk.account_state import empty_state
from app.risk.engine import evaluate, is_weekend
from app.risk.types import (
    AccountState,
    ExitReason,
    MarketState,
    OrderType,
    Proposal,
    ReasonCode,
    Side,
)
from pinned.risk_limits import RISK_LIMITS

pytestmark = pytest.mark.unit

NOW = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)  # a Tuesday, mid-session
EQUITY = 10_000.0


def make_proposal(
    *,
    entry: float = 4_000.0,
    sl: float | None = 3_900.0,      # 250 bps, clear of the cost floor
    tp: float | None = 4_200.0,      # 2R
    units: float = 0.05,
    side: Side = Side.LONG,
    rr: float = 2.0,
    risk_usd: float = 2.0,
) -> Proposal:
    return Proposal(
        proposal_id="p1",
        strategy_version_id=1,
        instrument="XAU_USD",
        side=side,
        order_type=OrderType.MARKET,
        units=units,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        expected_rr=rr,
        expected_risk_usd=risk_usd,
        bar_ts=NOW,
        client_tag="te-test",
        indicators={"atr": 120.0},
    )


def make_state(**overrides: object) -> AccountState:
    base = empty_state(EQUITY)
    return base.model_copy(update=overrides)


MARKET = MarketState(
    instrument="XAU_USD",
    granularity="M15",
    last_bar_ts=NOW,
    last_close=4_000.0,
    indicators={"atr": 120.0},
    seconds_since_last_bar=10,
)


def run(state: AccountState, proposal: Proposal | None = None) -> object:
    return evaluate(proposal or make_proposal(), state, RISK_LIMITS, MARKET, NOW)


# ------------------------------------------------------------------- baseline


def test_a_clean_proposal_is_approved() -> None:
    decision = run(make_state())
    assert decision.decision == "approved"
    assert decision.order is not None
    assert decision.order.stop_loss == 3_900.0
    assert decision.reason_codes == [ReasonCode.OK]


# ---------------------------------------------------------------- hard brakes


def test_kill_switch_blocks_everything() -> None:
    decision = run(make_state(is_kill_switch=True, kill_switch_reason="weekly loss"))
    assert decision.decision == "rejected"
    assert ReasonCode.KILL_SWITCH_ENGAGED in decision.reason_codes


def test_daily_loss_is_measured_against_start_of_day_equity() -> None:
    """A winning morning must not enlarge the afternoon's permitted loss."""
    # Equity high is far above the day's start, but the limit follows the day.
    state = make_state(
        equity_high=20_000.0,
        day_start_equity=10_000.0,
        day_pnl=-RISK_LIMITS.max_daily_loss_pct * 10_000.0,
    )
    decision = run(state)
    assert decision.decision == "skipped"
    assert ReasonCode.RISK_EXCEEDS_DAILY_LIMIT in decision.reason_codes


def test_weekly_loss_rejects() -> None:
    state = make_state(
        week_start_equity=10_000.0,
        week_pnl=-RISK_LIMITS.max_weekly_loss_pct * 10_000.0 - 1,
    )
    assert ReasonCode.RISK_EXCEEDS_WEEKLY_LIMIT in run(state).reason_codes


def test_drawdown_rejects() -> None:
    state = make_state(drawdown_pct=RISK_LIMITS.max_drawdown_pct)
    assert ReasonCode.RISK_EXCEEDS_MAX_DRAWDOWN in run(state).reason_codes


# ----------------------------------------------------------------- discipline


def test_weekend_blocks_entries() -> None:
    assert ReasonCode.MARKET_CLOSED in run(make_state(is_weekend=True)).reason_codes


def test_max_trades_per_day() -> None:
    state = make_state(trades_today=RISK_LIMITS.max_trades_per_day)
    decision = run(state)
    assert decision.decision == "skipped"
    assert ReasonCode.MAX_TRADES_PER_DAY in decision.reason_codes


def test_max_losses_per_day_ends_the_day() -> None:
    state = make_state(losses_today=RISK_LIMITS.max_losses_per_day)
    assert ReasonCode.MAX_LOSSES_PER_DAY in run(state).reason_codes


def test_consecutive_loss_cooldown() -> None:
    state = make_state(
        consecutive_losses=RISK_LIMITS.cooldown_after_consecutive_losses,
        bars_since_last_exit=1,
    )
    assert ReasonCode.CONSECUTIVE_LOSS_COOLDOWN in run(state).reason_codes


def test_consecutive_loss_cooldown_expires() -> None:
    state = make_state(
        consecutive_losses=RISK_LIMITS.cooldown_after_consecutive_losses,
        bars_since_last_exit=RISK_LIMITS.cooldown_after_consecutive_losses_bars,
    )
    assert run(state).decision == "approved"


def test_no_immediate_re_entry_after_a_stop_out() -> None:
    state = make_state(last_exit_reason=ExitReason.SL, bars_since_last_exit=1)
    assert ReasonCode.STOP_OUT_COOLDOWN in run(state).reason_codes


def test_same_direction_re_entry_after_a_loss_is_blocked() -> None:
    state = make_state(
        last_exit_reason=ExitReason.TIME_STOP,
        last_exit_was_loss=True,
        last_exit_side=Side.LONG,
        bars_since_last_exit=1,
    )
    assert ReasonCode.REENTRY_AFTER_LOSS_COOLDOWN in run(state).reason_codes


def test_minimum_spacing_between_entries() -> None:
    state = make_state(bars_since_last_entry=0)
    assert ReasonCode.MIN_BARS_BETWEEN_ENTRIES in run(state).reason_codes


def test_an_open_position_blocks_a_second_entry() -> None:
    assert ReasonCode.MAX_POSITIONS_REACHED in run(make_state(open_positions=1)).reason_codes


def test_shorts_are_refused() -> None:
    proposal = make_proposal(side=Side.SHORT, sl=4_040.0, tp=3_920.0)
    assert ReasonCode.SHORT_NOT_SUPPORTED in run(make_state(), proposal).reason_codes


def test_stale_data_skips() -> None:
    stale = MARKET.model_copy(
        update={"seconds_since_last_bar": RISK_LIMITS.stale_data_max_age_sec + 1}
    )
    decision = evaluate(make_proposal(), make_state(), RISK_LIMITS, stale, NOW)
    assert ReasonCode.STALE_DATA in decision.reason_codes


# ---------------------------------------------------------------- trade shape


def test_a_proposal_without_a_stop_is_rejected() -> None:
    decision = run(make_state(), make_proposal(sl=None))
    assert decision.decision == "rejected"
    assert ReasonCode.MANDATORY_STOP_LOSS_MISSING in decision.reason_codes


def test_a_stop_too_tight_to_pay_costs_is_rejected() -> None:
    # 5 bps of stop on a 4000 price hands the broker ten times the risk in fees.
    decision = run(make_state(), make_proposal(sl=3_998.0, tp=4_006.0, risk_usd=0.1))
    assert ReasonCode.SL_TOO_TIGHT_FOR_COSTS in decision.reason_codes


def test_a_stop_wider_than_three_atr_is_rejected() -> None:
    # 1000 wide against an ATR of 120 is well past the 3-ATR ceiling.
    decision = run(make_state(), make_proposal(sl=3_000.0, tp=6_000.0, risk_usd=1.0))
    assert ReasonCode.SL_OUT_OF_RANGE in decision.reason_codes


def test_rr_below_the_floor_is_rejected() -> None:
    decision = run(make_state(), make_proposal(rr=1.0))
    assert ReasonCode.MIN_RR_BELOW_FLOOR in decision.reason_codes


def test_per_trade_risk_cap() -> None:
    too_much = RISK_LIMITS.max_risk_per_trade_pct * EQUITY + 1.0
    decision = run(make_state(), make_proposal(risk_usd=too_much))
    assert ReasonCode.RISK_EXCEEDS_PER_TRADE in decision.reason_codes


def test_margin_cap() -> None:
    # 1 unit at 4000 with leverage 1.0 is far past 20% of a 10k account.
    decision = run(make_state(), make_proposal(units=1.0))
    assert ReasonCode.RISK_EXCEEDS_POSITION_VALUE in decision.reason_codes


# ------------------------------------------------------------------- sessions


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 3, 6, 20, 0, tzinfo=UTC), False),  # Friday, before close
        (datetime(2026, 3, 6, 21, 0, tzinfo=UTC), True),   # Friday, after close
        (datetime(2026, 3, 7, 12, 0, tzinfo=UTC), True),   # Saturday
        (datetime(2026, 3, 8, 21, 0, tzinfo=UTC), True),   # Sunday, before open
        (datetime(2026, 3, 8, 22, 0, tzinfo=UTC), False),  # Sunday, after open
        (datetime(2026, 3, 9, 12, 0, tzinfo=UTC), False),  # Monday
    ],
)
def test_weekend_window(moment: datetime, expected: bool) -> None:
    assert is_weekend(moment, RISK_LIMITS) is expected


# ------------------------------------------------------------------ properties


@settings(max_examples=200, deadline=None)
@given(
    trades_today=st.integers(min_value=0, max_value=10),
    losses_today=st.integers(min_value=0, max_value=10),
    open_positions=st.integers(min_value=0, max_value=3),
)
def test_no_approval_when_any_frequency_cap_is_reached(
    trades_today: int, losses_today: int, open_positions: int
) -> None:
    state = make_state(
        trades_today=trades_today,
        losses_today=losses_today,
        open_positions=open_positions,
        bars_since_last_entry=99,
        bars_since_last_exit=99,
    )
    decision = evaluate(make_proposal(), state, RISK_LIMITS, MARKET, NOW)
    capped = (
        trades_today >= RISK_LIMITS.max_trades_per_day
        or losses_today >= RISK_LIMITS.max_losses_per_day
        or open_positions >= RISK_LIMITS.max_open_positions
    )
    if capped:
        assert decision.decision != "approved"


@settings(max_examples=100, deadline=None)
@given(day_pnl=st.floats(min_value=-2_000.0, max_value=0.0))
def test_daily_loss_limit_is_a_hard_boundary(day_pnl: float) -> None:
    state = make_state(day_start_equity=EQUITY, day_pnl=day_pnl)
    decision = evaluate(make_proposal(), state, RISK_LIMITS, MARKET, NOW)
    if day_pnl <= -RISK_LIMITS.max_daily_loss_pct * EQUITY:
        assert decision.decision != "approved"


def test_an_approved_order_always_carries_a_stop() -> None:
    decision = run(make_state())
    assert decision.order is not None
    assert decision.order.stop_loss < decision.order.entry_price
    assert decision.order.take_profit > decision.order.entry_price
    assert decision.order.sl_distance > 0


def test_cooldowns_are_measured_in_bars_not_wall_clock() -> None:
    """A gap in the data must not silently expire a cooldown."""
    state = make_state(last_exit_reason=ExitReason.SL, bars_since_last_exit=0)
    later = NOW + timedelta(days=3)
    decision = evaluate(make_proposal(), state, RISK_LIMITS, MARKET, later)
    assert ReasonCode.STOP_OUT_COOLDOWN in decision.reason_codes


def test_the_cost_floor_bounds_what_fees_can_take_from_a_trade() -> None:
    """The stop width alone decides the fee's share of risk.

    fee_R = round_trip_bps / stop_width_bps, because risk-based sizing makes
    notional inversely proportional to the stop distance. The floor must keep
    that under a quarter of the risk on every trade the engine approves.
    """
    from pinned.simulation_constants import SIM

    round_trip_bps = 2 * SIM.fee_bps
    worst_case_fee_r = round_trip_bps / RISK_LIMITS.min_sl_distance_bps
    assert worst_case_fee_r <= 0.25


def test_a_stop_below_the_cost_floor_is_rejected() -> None:
    entry = 4_000.0
    floor = RISK_LIMITS.min_sl_distance_bps
    too_tight = entry * (1 - (floor - 1) / 10_000.0)
    proposal = make_proposal(
        entry=entry,
        sl=too_tight,
        tp=entry + 2 * (entry - too_tight),
        units=0.001,
        risk_usd=0.1,
    )
    market = MARKET.model_copy(update={"indicators": {"atr": 400.0}})
    decision = evaluate(proposal, make_state(), RISK_LIMITS, market, NOW)
    assert ReasonCode.SL_TOO_TIGHT_FOR_COSTS in decision.reason_codes


def test_a_stop_at_the_cost_floor_is_accepted() -> None:
    entry = 4_000.0
    wide = entry * (1 - (RISK_LIMITS.min_sl_distance_bps + 10) / 10_000.0)
    proposal = make_proposal(
        entry=entry,
        sl=wide,
        tp=entry + 2 * (entry - wide),
        units=0.001,
        risk_usd=0.1,
    )
    market = MARKET.model_copy(update={"indicators": {"atr": 400.0}})
    decision = evaluate(proposal, make_state(), RISK_LIMITS, market, NOW)
    assert decision.decision == "approved"
