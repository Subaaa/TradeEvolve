"""Property-based tests for the position sizing model."""
from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from app.risk.sizing import floor_to_lot, margin_required, size_position
from pinned.risk_limits import RISK_LIMITS
from pinned.simulation_constants import SimulationConstants


@given(
    qty=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
    step=st.sampled_from([0.001, 0.01, 0.1, 1.0]),
)
def test_floor_to_lot_never_rounds_up(qty: float, step: float) -> None:
    out = floor_to_lot(qty, step)
    assert out <= qty + 1e-9
    assert out >= 0.0
    # The result is an exact multiple of the step.
    assert math.isclose(out / step, round(out / step), abs_tol=1e-6)


def test_floor_to_lot_survives_binary_float_error() -> None:
    # 0.012 / 0.001 == 11.999999999999998 in IEEE754.
    assert floor_to_lot(0.012, 0.001) == 0.012


@given(
    equity=st.floats(min_value=1.0, max_value=10_000_000.0, allow_nan=False),
    entry=st.floats(min_value=1.0, max_value=100_000.0, allow_nan=False),
    sl_distance=st.floats(min_value=0.01, max_value=10_000.0, allow_nan=False),
    leverage=st.sampled_from([1.0, 10.0, 100.0, 500.0]),
)
@settings(max_examples=300, deadline=None)
def test_size_never_breaches_its_caps(
    equity: float, entry: float, sl_distance: float, leverage: float
) -> None:
    sim = SimulationConstants(leverage=leverage)
    qty = size_position(
        equity=equity, entry=entry, sl_distance=sl_distance, limits=RISK_LIMITS, sim=sim
    )
    if qty == 0.0:
        return
    risk = qty * sl_distance * sim.contract_size
    assert risk <= RISK_LIMITS.max_risk_per_trade_pct * equity + 1e-6
    margin = margin_required(qty, entry, sim)
    assert margin <= RISK_LIMITS.max_margin_per_position_pct * equity + 1e-6
    assert qty <= RISK_LIMITS.max_units + 1e-9


@given(
    equity=st.floats(min_value=100.0, max_value=1_000_000.0, allow_nan=False),
    entry=st.floats(min_value=100.0, max_value=10_000.0, allow_nan=False),
    sl_distance=st.floats(min_value=1.0, max_value=500.0, allow_nan=False),
)
@settings(max_examples=200, deadline=None)
def test_more_leverage_never_shrinks_the_position(
    equity: float, entry: float, sl_distance: float
) -> None:
    """Leverage relaxes the margin cap only; it must never reduce size."""
    low = size_position(
        equity=equity, entry=entry, sl_distance=sl_distance, sim=SimulationConstants(leverage=1.0)
    )
    high = size_position(
        equity=equity, entry=entry, sl_distance=sl_distance, sim=SimulationConstants(leverage=100.0)
    )
    assert high >= low


def test_degenerate_inputs_return_zero() -> None:
    assert size_position(equity=0.0, entry=4000.0, sl_distance=80.0) == 0.0
    assert size_position(equity=1000.0, entry=0.0, sl_distance=80.0) == 0.0
    assert size_position(equity=1000.0, entry=4000.0, sl_distance=0.0) == 0.0


def test_small_account_can_still_take_a_position() -> None:
    """The regression this model exists for: a $100 account on $4k gold."""
    qty = size_position(equity=100.0, entry=4289.0, sl_distance=83.0)
    assert qty > 0.0
    assert margin_required(qty, 4289.0) <= 0.20 * 100.0 + 1e-9


def test_leverage_lifts_the_small_account_to_its_risk_cap() -> None:
    """At leverage 1.0 margin binds; with leverage the risk cap binds instead."""
    lev = SimulationConstants(leverage=100.0)
    qty = size_position(equity=100.0, entry=4289.0, sl_distance=83.0, sim=lev)
    risk = qty * 83.0
    assert risk <= RISK_LIMITS.max_risk_per_trade_pct * 100.0 + 1e-9
    # Within one lot step of the full risk budget.
    assert risk >= RISK_LIMITS.max_risk_per_trade_pct * 100.0 - 83.0 * lev.min_lot_step
