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
    # The Alpaca paper account has no commission on crypto spot; we keep this for live prep.
    fee_bps: float = 0.0
    # H1 entries are mostly limit-style; 0.5bp is a conservative default.
    slippage_bps: float = 0.5
    # Conservative default for the bid/ask spread fallback when not provided.
    default_spread_bps: float = 2.0
    # --- Instrument / broker sizing model ---
    # Ounces of gold per 1 unit of `units`. Alpaca's PAXG/USD is 1 token =
    # 1 troy oz, so this is 1.0. An MT5-style broker quoting XAUUSD in lots
    # of 100 oz would set this to 100.0 and size in lots instead.
    contract_size: float = 1.0
    # Smallest tradeable increment of `units`. Alpaca accepts fractional
    # crypto quantities; 0.001 oz keeps sizing usable on small accounts.
    # An MT5 broker with a 0.01-lot minimum would set this to 0.01.
    min_lot_step: float = 0.001
    # Account leverage. Alpaca does NOT offer leverage on crypto (PAXG is
    # non-marginable), so 1.0 is the honest default for the current broker.
    # Raise it only alongside a broker that actually grants margin; the
    # margin cap in RISK_LIMITS is what bounds the resulting exposure.
    leverage: float = 1.0
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
