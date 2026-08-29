"""StrategyConfig schema and helpers.

Adding a new field to `StrategyConfig` is a human-only code change.
The LLM may only modify the values of fields whose names appear in
`MUTABLE_BY_LLM`.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field


# The LLM may only mutate the *value* of fields whose name is in this set.
# Adding a name here is a human-only change.
MUTABLE_BY_LLM: frozenset[str] = frozenset({
    "fast_ema_length",
    "slow_ema_length",
    "atr_length",
    "atr_sl_multiplier",
    "atr_tp_multiplier",
    "time_stop_hours",
})


class StrategyConfig(BaseModel):
    """Baseline EMA-crossover-with-ATR config.

    Every field is documented. The defaults match
    `pinned/strategies/ema_atr_xauusd_h1_v1.json`.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    version_tag: str
    instrument: str
    granularity: Literal["M1", "M5", "M15", "M30", "H1", "H4", "D"]
    fast_ema_length: int = Field(ge=2, le=200)
    slow_ema_length: int = Field(ge=2, le=400)
    atr_length: int = Field(ge=2, le=100)
    atr_percentile_window: int = Field(ge=20, le=2000)
    atr_percentile_max: float = Field(ge=0.5, le=0.999)
    atr_sl_multiplier: float = Field(gt=0.0, le=10.0)
    atr_tp_multiplier: float = Field(gt=0.0, le=20.0)
    min_rr_ratio: float = Field(ge=1.0, le=10.0)
    time_stop_hours: int = Field(ge=1, le=240)
    time_stop_min_r: float = Field(ge=0.0, le=5.0)
    weekend_policy: Literal["flatten", "hold"] = "flatten"

    # ---- invariants ----
    def model_post_init(self, __context) -> None:  # type: ignore[no-untyped-def]
        if self.slow_ema_length <= self.fast_ema_length:
            raise ValueError(
                f"slow_ema_length ({self.slow_ema_length}) must be > "
                f"fast_ema_length ({self.fast_ema_length})"
            )

    # ---- helpers ----
    def canonical_hash(self) -> bytes:
        """Stable SHA-256 of the canonicalized config."""
        payload = self.model_dump_json()
        return hashlib.sha256(payload.encode("utf-8")).digest()

    def is_field_mutable_by_llm(self, field_name: str) -> bool:
        return field_name in MUTABLE_BY_LLM

    @classmethod
    def from_json_file(cls, path: Path) -> "StrategyConfig":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(**data["params"], version_tag=data["version_tag"], instrument=data["instrument"], granularity=data["granularity"])
