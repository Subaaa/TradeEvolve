"""Hard risk ceilings — the boundaries the LLM can never weaken.

Every other component imports from this module; that no non-`pinned/` module
can write here is enforced by `test/test_pinned_invariant.py`.

All values are configurable by a human via this file only — never by an LLM
call, never by an env var, never by a config table.

Emotion-proof invariants
------------------------
These seven rules are the reason this file exists. Each is enforced by a
named gate in `app.risk.engine.evaluate` or by `app.execution.position_manager`,
and each has a test in `test/unit/risk/` that fails if the enforcement is
removed.

1.  **Every position is protected.** A position without a broker-resident
    stop order never survives more than one monitor cycle: if the protective
    stop cannot be placed, the position is flattened immediately.
2.  **A losing day ends the day.** Hitting `max_losses_per_day` or
    `max_daily_loss_pct` engages the kill switch until the next UTC day.
    There is no "one more trade to get it back".
3.  **No revenge.** After a stop-out, `cooldown_after_stop_out_bars` must
    elapse; after any loss, a same-direction re-entry is blocked for
    `reentry_after_loss_same_direction_bars`.
4.  **No chasing.** An entry may only act on the signal of the bar that just
    closed. A signal that was not acted on immediately is discarded, never
    carried to a later tick at a worse price.
5.  **No averaging down, no adds.** One position at a time, ever. A second
    entry while a position is open is refused by two independent gates.
6.  **Size is computed, never chosen.** The quantity follows from the stop
    distance and `max_risk_per_trade_pct`. There is no discretionary sizing.
7.  **The hard brakes need a human.** Weekly-loss and drawdown kill switches
    can only be cleared from the CLI by a person. The LLM has no code path
    that reaches any value in this file.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    """Frozen dataclass: a copy is what the RiskEngine actually receives.

    Mutating the module-level singleton would be a code change.
    """

    # --- Per-trade risk ---
    max_risk_per_trade_pct: float = 0.005          # 0.5% of equity
    # Cap on the MARGIN a single position ties up, not its notional. With
    # leverage 1.0 these are identical; above 1.0 the notional may exceed
    # equity, which is the whole point of margin. Bounding margin (not
    # notional) is what lets a small account take a correctly-sized
    # risk-based position instead of being floored to zero.
    max_margin_per_position_pct: float = 0.20      # 20% of equity as margin
    max_units: float = 100.0                       # hard cap regardless of sizing

    # --- Loss limits ---
    # Daily loss is measured against the equity recorded at 00:00 UTC, NOT
    # against the equity high-water mark. Measuring against the high-water
    # mark silently grows the daily budget on a winning day, which is exactly
    # the behaviour a disciplined trader does not want.
    max_daily_loss_pct: float = 0.02               # 2% of start-of-day equity
    max_weekly_loss_pct: float = 0.05              # 5% of start-of-week equity
    max_drawdown_pct: float = 0.10                 # 10% from equity high

    # --- Portfolio ---
    max_open_positions: int = 1                    # single asset, single position
    no_add_to_position: bool = True                # never scale in, never average down
    allow_short: bool = False                      # Alpaca crypto cannot be sold short

    # --- Trade shape ---
    min_rr_ratio: float = 1.5                      # reject below this R/R
    mandatory_stop_loss: bool = True               # reject proposals without SL
    max_sl_distance_atr_multiple: float = 3.0      # SL must be within 3 ATR
    # The cost floor, and the single most important number in this file.
    #
    # A round trip costs `2 * SIM.fee_bps` of NOTIONAL (50 bps on Alpaca
    # crypto). Risk-based sizing sets units = risk_usd / stop_distance, so the
    # fee expressed in R is:
    #
    #     fee_R = notional * 50bps / (units * stop_distance)
    #           = 50bps / stop_width_in_bps
    #
    # The stop width alone therefore decides what fraction of each trade's
    # risk is burned on costs, independent of account size, price and
    # strategy. A 50 bps stop hands the broker a full 1R per trade; no win
    # rate recovers that. Requiring 200 bps caps costs at 0.25R.
    #
    # This is why the number is here and not in `StrategyConfig`: it is a
    # solvency constraint, not a tuning parameter, and the LLM must not be
    # able to trade its way around it.
    min_sl_distance_bps: float = 200.0

    # --- Frequency / discipline (all measured in M15 bars) ---
    max_trades_per_day: int = 4
    max_losses_per_day: int = 2
    cooldown_after_consecutive_losses: int = 3     # 3 losses in a row → cooldown
    cooldown_after_consecutive_losses_bars: int = 16   # 4h
    cooldown_after_stop_out_bars: int = 4              # 1h after any stop-out
    min_bars_between_entries: int = 2                  # 30m
    reentry_after_loss_same_direction_bars: int = 8    # 2h

    # --- Position lifetime ---
    max_position_hold_bars: int = 96               # 24h hard cap

    # --- Kill-switch policy ---
    daily_loss_engages_kill_switch_until_next_utc_day: bool = True
    weekly_loss_requires_manual_clear: bool = True
    drawdown_requires_manual_clear: bool = True
    flatten_open_position_on_kill_switch: bool = True

    # --- Sessions ---
    # PAXG trades 24/7, but it tracks spot gold, which does not. Weekend
    # liquidity is thin and the spread widens; we simply do not enter.
    no_entries_on_weekend: bool = True
    weekend_start_weekday: int = 4                 # Friday
    weekend_start_hour_utc: int = 21
    weekend_end_weekday: int = 6                   # Sunday
    weekend_end_hour_utc: int = 22

    # --- Execution quality ---
    # Worse than this versus the decision close is journaled as a risk event.
    max_entry_slippage_bps: float = 15.0

    # --- Data freshness ---
    stale_data_max_age_sec: int = 180              # 3 min


# Module-level singleton. Other modules import this name, not the class.
RISK_LIMITS = RiskLimits()
