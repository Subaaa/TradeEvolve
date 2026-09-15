"""StrategyConfig schema and the LLM mutation allow-list.

Adding a field to `StrategyConfig` is a human-only code change. The LLM may
only change the *value* of a field whose name is in `MUTABLE_BY_LLM`, and
only within the bounds in `LLM_RANGES` — which are deliberately narrower
than the pydantic field bounds, so that a validated config is still not
necessarily a config the LLM was allowed to propose.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pinned.risk_limits import RISK_LIMITS

# The LLM may only mutate the value of fields whose name is in this set.
# Adding a name here is a human-only change.
MUTABLE_BY_LLM: frozenset[str] = frozenset(
    {
        "fast_ema_length",
        "slow_ema_length",
        "atr_length",
        "pullback_lookback",
        "atr_sl_multiplier",
        "tp_r_multiple",
        "time_stop_bars",
    }
)

# Inclusive (min, max) the LLM must stay within for each mutable field.
LLM_RANGES: dict[str, tuple[float, float]] = {
    "fast_ema_length": (5, 20),
    "slow_ema_length": (15, 60),
    "atr_length": (7, 30),
    "pullback_lookback": (2, 8),
    "atr_sl_multiplier": (1.0, 3.0),
    "tp_r_multiple": (1.5, 4.0),
    "time_stop_bars": (4, 48),
}

# Fields that must stay integral when a candidate is applied.
INTEGER_FIELDS: frozenset[str] = frozenset(
    {"fast_ema_length", "slow_ema_length", "atr_length", "pullback_lookback", "time_stop_bars"}
)


class StrategyConfig(BaseModel):
    """Long-only EMA-trend pullback-resumption config for M15.

    The defaults match `pinned/strategies/pullback_paxg_m15_v1.json`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version_tag: str
    instrument: str = "XAU_USD"
    granularity: Literal["M1", "M5", "M15", "M30", "H1", "H4", "D"] = "M15"

    # --- trend ---
    fast_ema_length: int = Field(default=9, ge=3, le=50)
    slow_ema_length: int = Field(default=21, ge=5, le=200)

    # --- volatility ---
    atr_length: int = Field(default=14, ge=5, le=50)
    atr_percentile_window: int = Field(default=200, ge=50, le=2000)
    atr_percentile_max: float = Field(default=0.95, ge=0.5, le=0.999)
    # A market quieter than this cannot pay the round-trip fee. Human-only:
    # letting the LLM lower it would let it trade itself into a guaranteed
    # loss one basis point at a time.
    min_atr_bps: float = Field(default=12.0, ge=0.0, le=100.0)

    # --- entry shape ---
    pullback_lookback: int = Field(default=3, ge=1, le=10)

    # --- exits ---
    # 2.5, not 1.5: a 1.5-ATR stop on M15 PAXG is ~120 bps, below the 200 bps
    # that RISK_LIMITS.min_sl_distance_bps requires to keep fees under 0.25R.
    atr_sl_multiplier: float = Field(default=2.5, gt=0.0, le=5.0)
    tp_r_multiple: float = Field(default=2.0, ge=1.0, le=6.0)
    time_stop_bars: int = Field(default=16, ge=1, le=192)
    time_stop_min_r: float = Field(default=0.5, ge=0.0, le=3.0)

    # --- invariants the LLM cannot flip ---
    long_only: Literal[True] = True

    @model_validator(mode="after")
    def _check_invariants(self) -> StrategyConfig:
        if self.slow_ema_length <= self.fast_ema_length:
            raise ValueError(
                f"slow_ema_length ({self.slow_ema_length}) must be > "
                f"fast_ema_length ({self.fast_ema_length})"
            )
        if self.tp_r_multiple < RISK_LIMITS.min_rr_ratio:
            raise ValueError(
                f"tp_r_multiple ({self.tp_r_multiple}) must be >= "
                f"RISK_LIMITS.min_rr_ratio ({RISK_LIMITS.min_rr_ratio})"
            )
        return self

    # ---- helpers ----

    def canonical_hash(self) -> bytes:
        """Stable SHA-256 over the tuning parameters only.

        `version_tag` is excluded so that two versions with identical
        behaviour hash identically — which is what makes the hash useful for
        spotting a challenger that is not actually a change.
        """
        payload = self.model_dump(mode="json", exclude={"version_tag"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).digest()

    def canonical_hash_hex(self) -> str:
        return self.canonical_hash().hex()

    def is_field_mutable_by_llm(self, field_name: str) -> bool:
        return field_name in MUTABLE_BY_LLM

    def llm_visible_params(self) -> dict[str, Any]:
        """The mutable parameters and their allowed ranges, for the prompt."""
        return {
            name: {
                "value": getattr(self, name),
                "min": LLM_RANGES[name][0],
                "max": LLM_RANGES[name][1],
            }
            for name in sorted(MUTABLE_BY_LLM)
        }

    @classmethod
    def from_json_file(cls, path: Path) -> StrategyConfig:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        params = dict(data.get("params", {}))
        params["version_tag"] = data["version_tag"]
        params["instrument"] = data["instrument"]
        params["granularity"] = data["granularity"]
        return cls(**params)

    @classmethod
    def from_json_str(cls, text: str) -> StrategyConfig:
        return cls.model_validate_json(text)

    def to_json_str(self) -> str:
        return self.model_dump_json()
