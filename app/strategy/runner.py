"""The deterministic StrategyRunner.

Given a `MarketState` and a `StrategyConfig`, produce zero or more
`Proposal`s. The runner is a **pure function**: no I/O, no LLM, no
network. The backtester reuses this same function.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

import pandas as pd

from app.market.indicators.atr import atr, atr_percentile
from app.market.indicators.ema import ema, ema_cross
from app.risk.sizing import size_position
from app.risk.types import OrderType, Proposal, Side

from .configs import StrategyConfig


def _ema_cross_state(
    closes: pd.Series, fast: int, slow: int
) -> pd.Series:
    """Return the cross state: 1 (cross up), -1 (cross down), 0 (none)."""
    return ema_cross(closes, fast, slow)


def propose(
    candles: pd.DataFrame,
    state: pd.Series,
    config: StrategyConfig,
    *,
    equity: float,
    strategy_version_id: int,
    session_id: str,
) -> list[Proposal]:
    """Produce a proposal for the current bar, or [].

    Args:
        candles: a DataFrame indexed by datetime with columns o, h, l, c, v.
            Must include the current bar as the last row.
        state: the latest `MarketState.indicators` row (kept for compatibility;
            this runner recomputes its own indicators from `candles`).
        config: the strategy config.
        equity: paper equity used for sizing.
        strategy_version_id: the strategy version that produced this proposal.
        session_id: a session id used in the client_tag.

    Returns:
        Zero or one `Proposal`s. The MVP produces at most one open position
        at a time; we never open a new one while a previous one is still
        open. The market state carries the open-position flag.
    """
    if candles.empty:
        return []
    last = candles.iloc[-1]
    prev = candles.iloc[-2] if len(candles) >= 2 else None
    if prev is None:
        return []

    closes = candles["c"]
    highs = candles["h"]
    lows = candles["l"]

    e_fast = ema(closes, config.fast_ema_length)
    e_slow = ema(closes, config.slow_ema_length)
    a = atr(highs, lows, closes, config.atr_length)
    a_pct = atr_percentile(a, window=config.atr_percentile_window, level=config.atr_percentile_max)

    cross = _ema_cross_state(closes, config.fast_ema_length, config.slow_ema_length)
    last_cross = cross.iloc[-1]
    atr_last = a.iloc[-1]
    atr_pct_last = a_pct.iloc[-1]

    # The most recent bar must be a cross; otherwise no signal.
    if last_cross == 0:
        return []
    if pd.isna(atr_last) or pd.isna(atr_pct_last):
        return []
    if atr_pct_last >= config.atr_percentile_max:
        return []                              # vol-spike filter

    side: Literal[Side.LONG, Side.SHORT] = Side.LONG if last_cross == 1 else Side.SHORT
    entry = float(last["c"])

    if side == Side.LONG:
        sl = entry - config.atr_sl_multiplier * atr_last
        tp = entry + config.atr_tp_multiplier * atr_last
    else:
        sl = entry + config.atr_sl_multiplier * atr_last
        tp = entry - config.atr_tp_multiplier * atr_last

    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        return []

    rr = abs(tp - entry) / sl_distance
    if rr < config.min_rr_ratio:
        return []

    # Sizing satisfies the risk cap, the margin cap and `max_units` at once;
    # see `app.risk.sizing` for why margin rather than notional is the cap.
    units = size_position(equity=equity, entry=entry, sl_distance=sl_distance)
    if units <= 0.0:
        return []

    bar_ts = candles.index[-1]
    if isinstance(bar_ts, pd.Timestamp):
        bar_dt: datetime = bar_ts.to_pydatetime()
    else:
        bar_dt = bar_ts  # type: ignore[assignment]

    # Idempotency tag
    tag_src = f"{strategy_version_id}|{equity}|{entry}|{sl}|{tp}|{bar_dt.isoformat()}|{session_id}"
    client_tag = "nito-" + hashlib.sha256(tag_src.encode("utf-8")).hexdigest()[:24]

    return [
        Proposal(
            proposal_id=client_tag,
            strategy_version_id=strategy_version_id,
            instrument=config.instrument,
            side=side,
            order_type=OrderType.MARKET,
            units=units,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            expected_rr=rr,
            expected_risk_usd=units * sl_distance,
            bar_ts=bar_dt,
            client_tag=client_tag,
        )
    ]
