"""Simulation constants: spread, slippage, fees, market hours.

These are the assumptions the backtester and the OrderGateway share.
Changing any of these changes the historical backtest; treat them as
pinned configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Final


@dataclass(frozen=True)
class SimulationConstants:
    # OANDA fxTrade Practice has no commission; we keep this for live prep.
    fee_bps: float = 0.0
    # H1 entries are mostly limit-style; 0.5bp is a conservative default.
    slippage_bps: float = 0.5
    # Conservative default for the bid/ask spread fallback when not provided.
    default_spread_bps: float = 2.0
    # Position rounding
    position_round_units: int = 1                # 1 unit of XAU
    # Session gaps
    flatten_before_daily_rollover_min: int = 30
    # Friday flatten time (UTC). Spot gold has Friday close ~21:00 UTC.
    friday_flatten_time_utc: time = time(20, 55)
    # Spot gold weekly open: Sunday 22:00 UTC
    sunday_open_time_utc: time = time(22, 0)
    # Daily break start (UTC) — gold spot has a brief break ~21:00–22:00 UTC
    daily_break_start_utc: time = time(21, 0)
    daily_break_end_utc: time = time(22, 0)


SIM = SimulationConstants()

# --- Live trading guard ---
# The rest of the code reads this const; it is never set to True in the MVP.
LIVE_TRADING_ENABLED: Final[bool] = False
