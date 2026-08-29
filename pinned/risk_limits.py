"""Hard risk ceilings.

These are the boundaries the LLM can never weaken. Every other component
imports from this module; mutations are caught by `tests/test_pinned_invariant.py`.

Defaults are conservative. All values are configurable by a human via this
file only — never by an LLM call, never by an env var, never by a config
table.
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
    max_position_value_pct: float = 0.20           # 20% of equity in one position
    max_units: int = 100                            # hard cap regardless of sizing

    # --- Loss limits ---
    max_daily_loss_pct: float = 0.03                # 3% of equity; new entries blocked
    max_weekly_loss_pct: float = 0.06               # 6% of equity; kill switch
    max_drawdown_pct: float = 0.15                  # 15% from equity high; kill switch

    # --- Portfolio ---
    max_open_positions: int = 1                     # MVP: single asset
    max_correlated_exposure_pct: float = 1.00       # MVP: not used

    # --- Trade shape ---
    min_rr_ratio: float = 1.5                       # reject below this R/R
    mandatory_stop_loss: bool = True                # reject proposals without SL
    max_sl_distance_atr_multiple: float = 3.0       # SL must be within ±3 ATR

    # --- Cooldowns ---
    cooldown_after_consecutive_losses: int = 3      # 3 losses → 24h cooldown
    cooldown_after_consecutive_loss_hours: int = 24
    cooldown_around_news_min: int = 30              # before AND after high-impact

    # --- Data freshness ---
    stale_data_max_age_sec: int = 300               # 5 min
    min_bar_volume_for_entry: int = 0               # XAU/USD volume is unreliable


# Module-level singleton. Other modules import this name, not the class.
RISK_LIMITS = RiskLimits()
