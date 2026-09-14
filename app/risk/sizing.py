"""Position sizing: turn a risk budget into a tradeable quantity.

Pure functions, no I/O. Shared by the StrategyRunner and the Backtester so
that "what size would we have taken" has exactly one answer.

The model has three independent caps and takes the smallest:

1. **Risk** — the stop must cost at most `max_risk_per_trade_pct` of equity.
2. **Margin** — the position must tie up at most `max_margin_per_position_pct`
   of equity *as margin*, i.e. notional / leverage. Capping notional directly
   (the pre-leverage behaviour) meant a 20% cap was really 0.2x leverage, which
   floored every XAU/USD signal to zero units once gold passed ~$2k against a
   small account.
3. **`max_units`** — a hard ceiling that ignores the arithmetic entirely.

The result is then floored to `min_lot_step`, because a broker will not fill
an arbitrary real number. Flooring (never rounding up) keeps every cap a true
upper bound.
"""
from __future__ import annotations

import math

from pinned.risk_limits import RISK_LIMITS, RiskLimits
from pinned.simulation_constants import SIM, SimulationConstants


def floor_to_lot(qty: float, step: float) -> float:
    """Round `qty` DOWN to a multiple of `step`.

    The epsilon absorbs binary-float error: 0.012 / 0.001 is 11.999999999999998
    in IEEE754, which would otherwise floor to 11 lots instead of 12.
    """
    if step <= 0.0:
        return max(0.0, qty)
    if qty <= 0.0:
        return 0.0
    lots = math.floor(qty / step + 1e-9)
    return round(lots * step, 10)


def size_position(
    *,
    equity: float,
    entry: float,
    sl_distance: float,
    limits: RiskLimits = RISK_LIMITS,
    sim: SimulationConstants = SIM,
) -> float:
    """Return the quantity to trade, or 0.0 if no lot-step-sized position fits.

    Args:
        equity: account equity in USD.
        entry: entry price per ounce.
        sl_distance: absolute price distance from entry to the stop, per ounce.
    """
    if equity <= 0.0 or entry <= 0.0 or sl_distance <= 0.0:
        return 0.0

    risk_per_unit = sl_distance * sim.contract_size
    notional_per_unit = entry * sim.contract_size
    if risk_per_unit <= 0.0 or notional_per_unit <= 0.0:
        return 0.0

    qty_by_risk = (equity * limits.max_risk_per_trade_pct) / risk_per_unit
    margin_per_unit = notional_per_unit / sim.leverage
    qty_by_margin = (equity * limits.max_margin_per_position_pct) / margin_per_unit

    qty = min(qty_by_risk, qty_by_margin, limits.max_units)
    return floor_to_lot(qty, sim.min_lot_step)


def margin_required(qty: float, entry: float, sim: SimulationConstants = SIM) -> float:
    """Margin in USD tied up by `qty` units at `entry`."""
    return abs(qty) * entry * sim.contract_size / sim.leverage
