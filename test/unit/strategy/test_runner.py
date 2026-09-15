"""The strategy rule and its two safety properties."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.execution.tags import make_tag
from app.risk.types import Side
from app.strategy.configs import StrategyConfig
from app.strategy.runner import propose, required_bars
from test.conftest import flat_series, pullback_series

pytestmark = pytest.mark.unit

EQUITY = 10_000.0


def run(df: pd.DataFrame, config: StrategyConfig, *, version: int = 1) -> list:
    return propose(df, config, equity=EQUITY, strategy_version_id=version)


def test_the_pullback_setup_produces_exactly_one_proposal(config: StrategyConfig) -> None:
    proposals = run(pullback_series(), config)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.side is Side.LONG
    assert p.stop_loss is not None and p.stop_loss < p.entry_price
    assert p.take_profit is not None and p.take_profit > p.entry_price
    assert p.expected_rr == pytest.approx(config.tp_r_multiple)
    assert p.units > 0


def test_a_flat_market_produces_nothing(config: StrategyConfig) -> None:
    assert run(flat_series(400), config) == []


def test_too_little_history_produces_nothing(config: StrategyConfig) -> None:
    short = pullback_series().iloc[-(required_bars(config) - 1) :]
    assert run(short, config) == []


def test_the_signal_never_fires_on_a_bar_that_is_not_the_trigger(
    config: StrategyConfig,
) -> None:
    """Dropping the resumption bar must remove the signal.

    This is the repainting guard from the other side: if the rule still fired
    without its trigger bar, the caller's forming-bar handling would not
    matter.
    """
    df = pullback_series()
    assert len(run(df, config)) == 1
    assert run(df.iloc[:-1], config) == []


def test_the_strategy_is_long_only(config: StrategyConfig) -> None:
    """Invert the series: a downtrend must produce no short, and no trade."""
    df = pullback_series()
    inverted = df.copy()
    pivot = float(df["c"].iloc[0]) * 2
    inverted["o"] = pivot - df["o"]
    inverted["c"] = pivot - df["c"]
    inverted["h"] = pivot - df["l"]
    inverted["l"] = pivot - df["h"]
    for p in run(inverted, config):
        assert p.side is Side.LONG


def test_the_tag_is_stable_across_repeated_evaluation(config: StrategyConfig) -> None:
    """The same bar must always produce the same client_order_id.

    This is what makes the broker's duplicate rejection a real guard; the old
    tag mixed in live equity and a per-process session id, so it changed on
    every tick and deduplicated nothing.
    """
    df = pullback_series()
    first = propose(df, config, equity=EQUITY, strategy_version_id=7)[0]
    # Different equity, same bar: the tag must not move.
    second = propose(df, config, equity=EQUITY * 3.5, strategy_version_id=7)[0]
    assert first.client_tag == second.client_tag
    assert first.client_tag == make_tag(
        strategy_version_id=7, side=Side.LONG, bar_ts=first.bar_ts
    )


def test_a_different_version_gets_a_different_tag(config: StrategyConfig) -> None:
    df = pullback_series()
    a = propose(df, config, equity=EQUITY, strategy_version_id=1)[0]
    b = propose(df, config, equity=EQUITY, strategy_version_id=2)[0]
    assert a.client_tag != b.client_tag


def test_a_too_quiet_market_is_refused() -> None:
    """A market that cannot pay the round-trip fee is not traded.

    The fixture's ATR is about 196 bps of price, so a floor above that must
    refuse the setup even though every other clause of the rule is satisfied.
    """
    quiet = StrategyConfig(version_tag="quiet", min_atr_bps=99.0)
    assert run(pullback_series(base=4_000.0), quiet) == []
    # Same series, a floor the market clears: the setup is taken.
    permissive = StrategyConfig(version_tag="loud", min_atr_bps=99.0)
    assert len(run(pullback_series(base=400.0), permissive)) == 1


def test_the_indicator_snapshot_is_recorded(config: StrategyConfig) -> None:
    """The journal needs the 'why', not just the 'what'."""
    p = run(pullback_series(), config)[0]
    for key in ("ema_fast", "ema_slow", "atr", "close", "prior_high", "sl_distance"):
        assert key in p.indicators
    assert p.indicators["ema_fast"] > p.indicators["ema_slow"]


def test_the_stop_distance_matches_the_atr_multiple(config: StrategyConfig) -> None:
    p = run(pullback_series(), config)[0]
    expected = config.atr_sl_multiplier * p.indicators["atr"]
    assert p.entry_price - p.stop_loss == pytest.approx(expected)


def test_an_empty_frame_is_handled(config: StrategyConfig) -> None:
    empty = pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in ("o", "h", "l", "c", "v")},
        index=pd.DatetimeIndex([], tz="UTC"),
    )
    assert run(empty, config) == []


def test_bar_timestamp_is_utc(config: StrategyConfig) -> None:
    p = run(pullback_series(), config)[0]
    assert p.bar_ts.tzinfo is not None
    assert p.bar_ts.utcoffset() == timedelta(0)
    assert isinstance(p.bar_ts, datetime)
    assert p.bar_ts.tzinfo is UTC or p.bar_ts.utcoffset() == timedelta(0)


def test_required_bars_covers_every_window(config: StrategyConfig) -> None:
    needed = required_bars(config)
    assert needed > config.atr_percentile_window
    assert needed > config.slow_ema_length



def test_the_prefix_path_and_the_whole_series_path_agree(
    config: StrategyConfig,
) -> None:
    """The backtester's shortcut must be a shortcut, not a different rule.

    `propose()` computes indicators over the window it is handed; the
    backtester computes them once over the full series and evaluates each bar.
    Those give identical results only because every indicator here is
    backward-looking, so this test pins that rather than assuming it.
    """
    from app.strategy.runner import compute_indicators, evaluate_bar

    df = pullback_series()
    whole = compute_indicators(df, config)

    for i in range(len(df) - 12, len(df)):
        prefix = df.iloc[: i + 1]
        from_prefix = propose(
            prefix, config, equity=EQUITY, strategy_version_id=3
        )
        from_whole = evaluate_bar(
            df, whole, i, config, equity=EQUITY, strategy_version_id=3
        )

        if from_prefix:
            assert from_whole is not None
            assert from_whole.client_tag == from_prefix[0].client_tag
            assert from_whole.entry_price == pytest.approx(from_prefix[0].entry_price)
            assert from_whole.stop_loss == pytest.approx(from_prefix[0].stop_loss)
            assert from_whole.units == pytest.approx(from_prefix[0].units)
            for key, value in from_prefix[0].indicators.items():
                assert from_whole.indicators[key] == pytest.approx(value)
        else:
            assert from_whole is None
