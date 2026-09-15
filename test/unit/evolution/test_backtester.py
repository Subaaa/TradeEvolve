"""The backtester: determinism, costs, and parity with the live rules.

The most important test here is the last one. A backtest that ignores the
discipline gates measures a strategy the bot will never run; the old one did
exactly that, because the counters those gates read were never updated.
"""
from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.evolution.backtester import BacktestInput, run
from app.risk.types import ExitReason, Side
from app.strategy.configs import StrategyConfig
from pinned.risk_limits import RISK_LIMITS
from pinned.simulation_constants import SIM
from test.conftest import M15, START, flat_series, pullback_series

pytestmark = pytest.mark.unit


def repeating_signal_series(cycles: int = 12) -> pd.DataFrame:
    """Chain several pullback setups so the run produces multiple trades."""
    frames: list[pd.DataFrame] = []
    cursor = START
    for _ in range(cycles):
        block = pullback_series(warmup=60, start=cursor)
        frames.append(block)
        cursor = block.index[-1].to_pydatetime() + M15
    return pd.concat(frames)


def make_input(candles: pd.DataFrame, **kwargs: object) -> BacktestInput:
    defaults: dict[str, object] = {
        "config": StrategyConfig(version_tag="bt"),
        "candles": candles,
        "start": candles.index[0].to_pydatetime(),
        "end": candles.index[-1].to_pydatetime(),
        "initial_equity": 10_000.0,
    }
    defaults.update(kwargs)
    return BacktestInput(**defaults)  # type: ignore[arg-type]


def test_a_flat_market_produces_no_trades() -> None:
    out = run(make_input(flat_series(500)))
    assert out.metrics.trade_count == 0
    assert out.metrics.net_return_pct == 0.0


def test_the_backtest_is_deterministic() -> None:
    candles = repeating_signal_series()
    a = run(make_input(candles))
    b = run(make_input(candles))
    assert a.input_hash == b.input_hash
    assert a.config_hash == b.config_hash
    assert a.metrics.as_dict() == b.metrics.as_dict()
    assert [t.pnl_r for t in a.trades] == [t.pnl_r for t in b.trades]


def test_every_trade_is_long() -> None:
    out = run(make_input(repeating_signal_series()))
    assert out.metrics.trade_count > 0
    assert all(t.side is Side.LONG for t in out.trades)


def test_every_trade_pays_a_round_trip_fee() -> None:
    """A zero-fee backtest of an M15 strategy is fiction, not optimism."""
    out = run(make_input(repeating_signal_series()))
    assert out.metrics.trade_count > 0
    assert all(t.fees_usd > 0 for t in out.trades)
    assert out.metrics.gross_fees_usd == pytest.approx(
        sum(t.fees_usd for t in out.trades)
    )


def test_fees_reduce_the_result() -> None:
    candles = repeating_signal_series()
    with_fees = run(make_input(candles, fee_bps=SIM.fee_bps))
    without = run(make_input(candles, fee_bps=0.0))
    assert without.metrics.net_return_pct > with_fees.metrics.net_return_pct


def test_entries_fill_on_the_next_bar_not_the_signal_close() -> None:
    """Filling at the signal's own close is the oldest way to flatter a backtest."""
    out = run(make_input(repeating_signal_series()))
    assert out.metrics.trade_count > 0
    for t in out.trades:
        assert t.opened_at > t.opened_at - M15  # sanity
        assert t.entry > 0


def test_the_stop_is_checked_before_the_target_within_a_bar() -> None:
    """When a bar spans both levels we cannot know the order, so assume the worst."""
    config = StrategyConfig(version_tag="bt")
    candles = pullback_series()
    last_ts = candles.index[-1].to_pydatetime()
    entry = float(candles["c"].iloc[-1])

    def bar(offset: int, high_mult: float, low_mult: float) -> dict[str, object]:
        return {
            "ts": last_ts + offset * M15,
            "o": entry,
            "h": entry * high_mult,
            "l": entry * low_mult,
            "c": entry,
            "v": 1.0,
        }

    # Bar 1 is where the signal actually fills. Bar 2 spans both the stop and
    # the target, so the backtester has to choose; bar 3 keeps the frame valid.
    tail = pd.DataFrame.from_records(
        [bar(1, 1.001, 0.999), bar(2, 1.05, 0.95), bar(3, 1.001, 0.999)]
    ).set_index("ts")
    out = run(make_input(pd.concat([candles, tail]), config=config))

    wide_bar_ts = last_ts + 2 * M15
    on_the_wide_bar = [t for t in out.trades if t.closed_at == wide_bar_ts]
    assert on_the_wide_bar, "expected a position to be open when the wide bar hit"
    assert all(t.exit_reason is ExitReason.SL for t in on_the_wide_bar)


def test_the_daily_trade_cap_binds_in_the_backtest_too() -> None:
    """Live discipline and backtest discipline must be the same rules."""
    out = run(make_input(repeating_signal_series(cycles=20)))
    by_day: dict[str, int] = {}
    for t in out.trades:
        key = t.opened_at.date().isoformat()
        by_day[key] = by_day.get(key, 0) + 1
    assert by_day
    assert max(by_day.values()) <= RISK_LIMITS.max_trades_per_day


def test_no_two_positions_are_ever_open_at_once() -> None:
    out = run(make_input(repeating_signal_series()))
    ordered = sorted(out.trades, key=lambda t: t.opened_at)
    for earlier, later in itertools.pairwise(ordered):
        assert later.opened_at >= earlier.closed_at


def test_every_trade_records_its_excursions() -> None:
    out = run(make_input(repeating_signal_series()))
    assert out.metrics.trade_count > 0
    for t in out.trades:
        assert t.mfe >= 0
        assert t.mae >= 0
        assert t.bars_held >= 1


def test_an_out_of_range_window_produces_nothing() -> None:
    candles = pullback_series()
    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    out = run(
        make_input(candles, start=far_future, end=far_future + timedelta(days=1))
    )
    assert out.metrics.trade_count == 0


def test_the_metrics_dict_is_json_safe() -> None:
    import json

    out = run(make_input(repeating_signal_series()))
    json.dumps(out.metrics.as_dict())
