"""Simulation constants: spread, slippage, fees, sizing model, sessions.

These are the assumptions the backtester and the live executor share.
Changing any of these changes every historical backtest; treat them as
pinned configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
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

    # Session boundaries live in `pinned/risk_limits.py` as
    # `no_entries_on_weekend` / `weekend_start_*` / `weekend_end_*`, enforced
    # by `app.risk.engine.is_weekend`. That is the only weekend rule: it
    # blocks new entries and nothing else. There used to be a second,
    # unused pair of weekend constants here and a matching force-flatten
    # rule in the backtester that the live `PositionManager` never
    # implemented — a backtest/live mismatch, removed rather than kept.


SIM = SimulationConstants()

# --- Live trading guard ---
# The rest of the code reads this const; it is never set to True in the MVP.
LIVE_TRADING_ENABLED: Final[bool] = False
