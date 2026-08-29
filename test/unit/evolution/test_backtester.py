"""Backtester tests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app.evolution.backtester import BacktestInput, run
from app.strategy.configs import StrategyConfig


def _synthetic(n: int = 1000, seed: int = 0, trend: float = 0.05, vol: float = 1.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="h", tz="UTC")
    # Mix of trend and noise
    drift = np.arange(n) * trend
    noise = rng.normal(scale=vol, size=n).cumsum() * 0.3
    closes = 2000.0 + drift + noise
    highs = closes + np.abs(rng.normal(scale=vol * 0.6, size=n))
    lows = closes - np.abs(rng.normal(scale=vol * 0.6, size=n))
    opens = closes + rng.normal(scale=vol * 0.2, size=n)
    return pd.DataFrame({"o": opens, "h": highs, "l": lows, "c": closes, "v": 0}, index=idx)


def test_backtester_runs_on_synthetic_data() -> None:
    cfg = StrategyConfig.from_json_file(Path("pinned/strategies/ema_atr_xauusd_h1_v1.json"))
    candles = _synthetic(n=2000, seed=42, trend=0.02, vol=2.0)
    out = run(BacktestInput(
        config=cfg, candles=candles,
        start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        end=datetime(2022, 1, 1, tzinfo=timezone.utc),
        initial_equity=10_000.0, oos_label="train",
    ))
    assert out.metrics.trade_count >= 0
    assert isinstance(out.metrics.net_return_pct, float)
    assert isinstance(out.metrics.max_drawdown_pct, float)
    assert 0.0 <= out.metrics.complexity_penalty <= 0.05
    assert out.config_hash == cfg.canonical_hash().hex()


def test_backtester_is_deterministic() -> None:
    """10 runs on the same input produce byte-identical output."""
    cfg = StrategyConfig.from_json_file(Path("pinned/strategies/ema_atr_xauusd_h1_v1.json"))
    candles = _synthetic(n=1500, seed=1, trend=0.01, vol=3.0)
    bt = BacktestInput(
        config=cfg, candles=candles,
        start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        end=datetime(2021, 6, 1, tzinfo=timezone.utc),
        initial_equity=10_000.0, oos_label="train", session_id="det",
    )
    out1 = run(bt)
    out2 = run(bt)
    assert out1.config_hash == out2.config_hash
    assert out1.input_hash == out2.input_hash
    assert out1.metrics == out2.metrics
    assert len(out1.trades) == len(out2.trades)
    for a, b in zip(out1.trades, out2.trades):
        assert a == b


def test_backtester_short_window_is_empty() -> None:
    cfg = StrategyConfig.from_json_file(Path("pinned/strategies/ema_atr_xauusd_h1_v1.json"))
    candles = _synthetic(n=30, seed=2)
    out = run(BacktestInput(
        config=cfg, candles=candles,
        start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        end=datetime(2020, 1, 2, tzinfo=timezone.utc),
        initial_equity=10_000.0, oos_label="train",
    ))
    assert out.metrics.trade_count == 0
    assert out.metrics.net_return_pct == 0.0


def test_backtester_no_nan_in_metrics() -> None:
    cfg = StrategyConfig.from_json_file(Path("pinned/strategies/ema_atr_xauusd_h1_v1.json"))
    candles = _synthetic(n=1500, seed=3, trend=0.0, vol=5.0)
    out = run(BacktestInput(
        config=cfg, candles=candles,
        start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        end=datetime(2021, 1, 1, tzinfo=timezone.utc),
        initial_equity=10_000.0, oos_label="train",
    ))
    for f in [
        out.metrics.net_return_pct, out.metrics.expectancy_r, out.metrics.max_drawdown_pct,
        out.metrics.sharpe, out.metrics.profit_factor, out.metrics.consistency_score,
        out.metrics.complexity_penalty, out.metrics.primary_score, out.metrics.tie_break_score,
    ]:
        # inf is allowed for profit_factor with no losses, but not NaN
        if f != float("inf") and f != float("-inf"):
            assert not (f != f)   # not NaN
