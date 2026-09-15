"""The deterministic StrategyRunner: EMA trend + pullback resumption, long only.

The rule is expressed once, in `evaluate_bar`, and reached two ways:

*   `propose()` — what the live tick calls. Computes indicators over the
    window it was handed and evaluates the last bar.
*   `compute_indicators()` + `evaluate_bar()` — what the backtester calls.
    Computes the indicators **once** over the whole series and evaluates each
    bar in turn.

Both paths run identical arithmetic, because EMA, ATR and the rolling ATR
percentile are all backward-looking: the value at bar `i` depends only on bars
up to `i`, so computing over a prefix and computing over the full series give
the same number. That equivalence is what makes the split safe, and
`test_runner.py` asserts it rather than assuming it. The alternative — having
the backtester recompute every indicator over a growing window on every bar —
is quadratic, and on a year of M15 data it makes the weekly evolution job take
hours instead of seconds.

Two properties matter more than the rule itself:

*   **It only ever sees closed bars.** The caller drops the forming bar
    (`app.market.bars.drop_forming_bar`); the runner treats the last row as
    final. The old implementation read a bar that was still forming, so a
    signal could appear mid-bar and later vanish — the classic repainting bug,
    which makes every backtest a lie.
*   **The idempotency tag depends only on the version, the side and the bar.**
    Re-running the same bar produces the same `client_order_id`, so the broker
    refuses the duplicate. The old tag mixed in live equity and a per-process
    session id, so it changed every tick and deduplicated nothing.

The rule, evaluated on closed bar B0 (B1 is the bar before it):

1. Trend: fast EMA above slow EMA, and the close above the slow EMA.
2. Pullback: price touched or pierced the fast EMA within the last
   `pullback_lookback` bars.
3. Resumption: the close is above B1's high and above the fast EMA.
4. Volatility is neither a spike (`atr_percentile_max`) nor too quiet to pay
   the round-trip fee (`min_atr_bps`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from app.execution.tags import make_tag
from app.market.indicators.atr import atr, atr_percentile
from app.market.indicators.ema import ema
from app.risk.sizing import size_position
from app.risk.types import OrderType, Proposal, Side

from .configs import StrategyConfig


@dataclass(frozen=True)
class Indicators:
    """Indicator series aligned to the candle frame they were built from."""

    ema_fast: np.ndarray
    ema_slow: np.ndarray
    atr: np.ndarray
    atr_pct: np.ndarray


def required_bars(config: StrategyConfig) -> int:
    """Minimum number of closed bars before the rule can be evaluated."""
    return (
        max(
            config.slow_ema_length,
            config.atr_length + 1,
            config.atr_percentile_window,
            config.pullback_lookback + 2,
        )
        + 2
    )


def compute_indicators(bars: pd.DataFrame, config: StrategyConfig) -> Indicators:
    """Compute every indicator the rule needs, once, over the whole frame."""
    closes = bars["c"]
    a = atr(bars["h"], bars["l"], closes, config.atr_length)
    return Indicators(
        ema_fast=ema(closes, config.fast_ema_length).to_numpy(dtype=float),
        ema_slow=ema(closes, config.slow_ema_length).to_numpy(dtype=float),
        atr=a.to_numpy(dtype=float),
        atr_pct=atr_percentile(
            a, window=config.atr_percentile_window, level=config.atr_percentile_max
        ).to_numpy(dtype=float),
    )


def _as_utc(ts: object) -> datetime:
    if isinstance(ts, pd.Timestamp):
        dt: datetime = ts.to_pydatetime()
    elif isinstance(ts, datetime):
        dt = ts
    else:  # pragma: no cover - the index is always datetime-like
        raise TypeError(f"unsupported bar timestamp type: {type(ts)!r}")
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def evaluate_bar(
    bars: pd.DataFrame,
    ind: Indicators,
    i: int,
    config: StrategyConfig,
    *,
    equity: float,
    strategy_version_id: int,
    highs: np.ndarray | None = None,
    lows: np.ndarray | None = None,
    closes: np.ndarray | None = None,
) -> Proposal | None:
    """Evaluate the rule at bar `i`. Returns a Proposal or None.

    The optional array arguments let a caller hoist the `to_numpy()` calls out
    of a loop; they are the same values the function would read anyway.
    """
    if i < required_bars(config) - 1:
        return None

    highs = bars["h"].to_numpy(dtype=float) if highs is None else highs
    lows = bars["l"].to_numpy(dtype=float) if lows is None else lows
    closes = bars["c"].to_numpy(dtype=float) if closes is None else closes

    fast = ind.ema_fast[i]
    slow = ind.ema_slow[i]
    atr_now = ind.atr[i]
    if np.isnan(fast) or np.isnan(slow) or np.isnan(atr_now) or atr_now <= 0:
        return None

    close_b0 = closes[i]
    high_b1 = highs[i - 1]

    # 1. Trend
    if not (fast > slow and close_b0 > slow):
        return None

    # 2. Pullback: did price reach the fast EMA in the recent window? The
    #    window ends at B0 inclusive, so a same-bar touch-and-reclaim counts.
    look = config.pullback_lookback
    lo = max(0, i - look + 1)
    if not bool(np.any(lows[lo : i + 1] <= ind.ema_fast[lo : i + 1])):
        return None

    # 3. Resumption trigger
    if not (close_b0 > high_b1 and close_b0 > fast):
        return None

    # 4. Volatility band
    atr_pct_now = ind.atr_pct[i]
    if not np.isnan(atr_pct_now) and atr_pct_now >= config.atr_percentile_max:
        return None
    atr_bps = atr_now / close_b0 * 10_000.0
    if atr_bps < config.min_atr_bps:
        return None

    # ---- shape the trade ----
    entry = float(close_b0)
    sl_distance = config.atr_sl_multiplier * float(atr_now)
    if sl_distance <= 0:
        return None
    sl = entry - sl_distance
    tp = entry + config.tp_r_multiple * sl_distance
    rr = (tp - entry) / sl_distance

    units = size_position(equity=equity, entry=entry, sl_distance=sl_distance)
    if units <= 0.0:
        return None

    bar_dt = _as_utc(bars.index[i])
    client_tag = make_tag(
        strategy_version_id=strategy_version_id, side=Side.LONG, bar_ts=bar_dt
    )

    indicators = {
        "ema_fast": float(fast),
        "ema_slow": float(slow),
        "atr": float(atr_now),
        "atr_pct": 0.0 if np.isnan(atr_pct_now) else float(atr_pct_now),
        "atr_bps": float(atr_bps),
        "close": entry,
        "prior_high": float(high_b1),
        "pullback_low": float(np.min(lows[lo : i + 1])),
        "sl_distance": sl_distance,
        "sl_bps": sl_distance / entry * 10_000.0,
    }

    return Proposal(
        proposal_id=client_tag,
        strategy_version_id=strategy_version_id,
        instrument=config.instrument,
        side=Side.LONG,
        order_type=OrderType.MARKET,
        units=units,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        expected_rr=rr,
        expected_risk_usd=units * sl_distance,
        bar_ts=bar_dt,
        client_tag=client_tag,
        indicators=indicators,
    )


def propose(
    closed_bars: pd.DataFrame,
    config: StrategyConfig,
    *,
    equity: float,
    strategy_version_id: int,
) -> list[Proposal]:
    """Produce at most one Proposal for the last closed bar, or [].

    Args:
        closed_bars: DataFrame indexed by UTC datetime with columns o,h,l,c,v.
            The last row must be a CLOSED bar.
        config: the strategy config.
        equity: account equity used for sizing.
        strategy_version_id: the version that produced this proposal.
    """
    if closed_bars.empty or len(closed_bars) < required_bars(config):
        return []

    ind = compute_indicators(closed_bars, config)
    proposal = evaluate_bar(
        closed_bars,
        ind,
        len(closed_bars) - 1,
        config,
        equity=equity,
        strategy_version_id=strategy_version_id,
    )
    return [proposal] if proposal is not None else []
