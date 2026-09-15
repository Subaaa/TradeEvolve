"""Simulation constants: spread, slippage, fees, sizing model, sessions.

These are the assumptions the backtester and the live executor share.
Changing any of these changes every historical backtest; treat them as
pinned configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Final


@dataclass(frozen=True)
class SimulationConstants:
    # --- Costs ---
    # Alpaca crypto taker fee at the entry tier is 25 bps per side. A
    # market-in / market-out round trip therefore costs ~50 bps of notional.
    # On M15 gold that is the dominant cost; modelling it as zero (the old
    # default) made every backtest a fiction.
    fee_bps: float = 25.0
    # Market IOC entries cross the spread; 2 bps is the observed PAXG/USD
    # half-spread in normal conditions.
    slippage_bps: float = 2.0
    default_spread_bps: float = 4.0
    # The protective stop is a stop_limit (Alpaca crypto has no plain stop).
    # The limit sits this far below the stop price so a fast move still
    # fills instead of leaving the position naked.
    stop_limit_band_bps: float = 50.0
    # Entries are immediate-or-cancel: we either get the price now or we
    # do not trade. This is what "no chasing" means at the order level.
    entry_tif: str = "ioc"

    # --- Instrument / broker sizing model ---
    # Ounces of gold per 1 unit of `units`. Alpaca's PAXG/USD is 1 token =
    # 1 troy oz, so this is 1.0.
    contract_size: float = 1.0
    # Smallest tradeable increment of `units`. Alpaca accepts fractional
    # crypto quantities; 0.001 oz keeps sizing usable on small accounts.
    min_lot_step: float = 0.001
    # Alpaca does NOT offer leverage on crypto (PAXG is non-marginable).
    leverage: float = 1.0

    # --- Sessions (gold spot weekly boundaries, applied to the PAXG proxy) ---
    friday_flatten_time_utc: time = time(20, 55)
    sunday_open_time_utc: time = time(22, 0)


SIM = SimulationConstants()

# --- Live trading guard ---
# The rest of the code reads this const; it is never set to True in the MVP.
LIVE_TRADING_ENABLED: Final[bool] = False
