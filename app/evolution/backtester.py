"""The deterministic Backtester.

This is the single source of truth for "what would have happened". It shares
`propose()` and `evaluate()` with the live loop, so the discipline gates that
block a live trade also block it here. That equivalence is the only reason a
backtest number means anything — a backtest that ignores the daily loss limit
and the trade-count cap is measuring a strategy the bot will never run.

Two things the old version got wrong and this one does not:

*   **Costs.** `fee_bps` defaulted to zero. On M15 gold a 50bps round trip is
    comparable to the entire move being traded, so a zero-fee backtest is not
    optimistic, it is unrelated. The default is now the real Alpaca taker fee.
*   **Discipline.** The old loop tracked nothing but the open position, so
    every gate that depends on today's trade count or the last exit was dead
    in the backtest exactly as it was live.

Pure function of its input: no clock, no I/O. A repeated run on the same
input produces byte-identical output.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.market.bars import granularity_seconds
from app.risk.account_state import empty_state, roll_day, roll_week, update_after_close
from app.risk.engine import evaluate, is_weekend
from app.risk.sizing import floor_to_lot
from app.risk.types import AccountState, ExitReason, MarketState, Side
from app.strategy.configs import StrategyConfig
from app.strategy.runner import compute_indicators, evaluate_bar, required_bars
from pinned.promotion_gates import PROMOTION_GATES, PromotionGates
from pinned.risk_limits import RISK_LIMITS, RiskLimits
from pinned.simulation_constants import SIM

# ---------- Types ----------------------------------------------------------


@dataclass(frozen=True)
class Trade:
    opened_at: datetime
    closed_at: datetime
    side: Side
    units: float
    entry: float
    sl: float
    tp: float
    exit_price: float
    pnl_usd: float
    pnl_r: float
    fees_usd: float
    mae: float
    mfe: float
    mae_r: float
    mfe_r: float
    bars_held: int
    exit_reason: ExitReason


@dataclass(frozen=True)
class BacktestInput:
    config: StrategyConfig
    candles: pd.DataFrame
    start: datetime
    end: datetime
    initial_equity: float
    fee_bps: float = SIM.fee_bps
    slippage_bps: float = SIM.slippage_bps
    risk_limits: RiskLimits = RISK_LIMITS
    oos_label: str = "train"
    session_id: str = "backtest"


@dataclass(frozen=True)
class PerformanceMetrics:
    net_return_pct: float
    expectancy_r: float
    max_drawdown_pct: float
    sharpe: float
    profit_factor: float
    trade_count: int
    consistency_score: float
    complexity_penalty: float
    gross_fees_usd: float = 0.0
    win_rate: float = 0.0
    primary_score: float = 0.0
    tie_break_score: float = 0.0
    exit_reason_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "net_return_pct": self.net_return_pct,
            "expectancy_r": self.expectancy_r,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe": self.sharpe,
            "profit_factor": self.profit_factor,
            "trade_count": self.trade_count,
            "consistency_score": self.consistency_score,
            "complexity_penalty": self.complexity_penalty,
            "gross_fees_usd": self.gross_fees_usd,
            "win_rate": self.win_rate,
            "primary_score": self.primary_score,
            "tie_break_score": self.tie_break_score,
            "exit_reason_counts": dict(self.exit_reason_counts),
        }


@dataclass(frozen=True)
class BacktestOutput:
    trades: tuple[Trade, ...]
    metrics: PerformanceMetrics
    config_hash: str
    input_hash: str
    oos_label: str


EMPTY_METRICS = PerformanceMetrics(
    net_return_pct=0.0,
    expectancy_r=0.0,
    max_drawdown_pct=0.0,
    sharpe=0.0,
    profit_factor=0.0,
    trade_count=0,
    consistency_score=0.0,
    complexity_penalty=0.0,
)


# ---------- Cost model -----------------------------------------------------


def _buy_price(price: float, slippage_bps: float) -> float:
    return price * (1.0 + slippage_bps / 10_000.0)


def _sell_price(price: float, slippage_bps: float) -> float:
    return price * (1.0 - slippage_bps / 10_000.0)


def _fee(notional: float, fee_bps: float) -> float:
    return abs(notional) * fee_bps / 10_000.0


# ---------- Metrics --------------------------------------------------------


def _max_drawdown(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        dd = (peak - e) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd


def _consistency_score(period_pnl: dict[str, float]) -> float:
    """Fraction of calendar weeks that were net positive.

    Weeks, not months: an M15 strategy evaluated over a 30-day fold has no
    months to speak of, and the old monthly bucketing collapsed to a single
    bucket, making the score a constant 1.0 or 0.0.
    """
    if not period_pnl:
        return 0.0
    positive = sum(1 for v in period_pnl.values() if v > 0)
    return positive / len(period_pnl)


def _primary_score(m: PerformanceMetrics) -> float:
    if m.trade_count == 0 or m.max_drawdown_pct <= 0:
        return 0.0
    return (
        m.expectancy_r
        * m.consistency_score
        / m.max_drawdown_pct
        * (1.0 - m.complexity_penalty)
    )


def _tie_break(m: PerformanceMetrics, gates: PromotionGates) -> float:
    pf = m.profit_factor if math.isfinite(m.profit_factor) else 10.0
    return gates.score_sharpe_weight * m.sharpe + gates.score_profit_factor_weight * pf


def compute_metrics(
    trades: tuple[Trade, ...],
    *,
    initial_equity: float,
    final_equity: float,
    equity_curve: list[float],
    period_pnl: dict[str, float],
    num_params: int,
    gates: PromotionGates,
) -> PerformanceMetrics:
    complexity = 0.005 * num_params
    if not trades:
        return PerformanceMetrics(
            net_return_pct=0.0,
            expectancy_r=0.0,
            max_drawdown_pct=0.0,
            sharpe=0.0,
            profit_factor=0.0,
            trade_count=0,
            consistency_score=0.0,
            complexity_penalty=complexity,
        )

    rs = np.array([t.pnl_r for t in trades], dtype=float)
    wins = rs[rs > 0]
    losses = rs[rs < 0]
    gross_win = float(wins.sum()) if wins.size else 0.0
    gross_loss = float(-losses.sum()) if losses.size else 0.0
    profit_factor = gross_win / gross_loss if gross_loss > 0 else math.inf
    std = float(rs.std(ddof=1)) if rs.size > 1 else 0.0
    sharpe = float(rs.mean() / std * math.sqrt(rs.size)) if std > 0 else 0.0

    exit_counts: dict[str, int] = {}
    for t in trades:
        exit_counts[t.exit_reason.value] = exit_counts.get(t.exit_reason.value, 0) + 1

    base = PerformanceMetrics(
        net_return_pct=(final_equity - initial_equity) / initial_equity
        if initial_equity > 0
        else 0.0,
        expectancy_r=float(rs.mean()),
        max_drawdown_pct=_max_drawdown(equity_curve),
        sharpe=sharpe,
        profit_factor=profit_factor,
        trade_count=len(trades),
        consistency_score=_consistency_score(period_pnl),
        complexity_penalty=complexity,
        gross_fees_usd=float(sum(t.fees_usd for t in trades)),
        win_rate=float(wins.size / rs.size),
        exit_reason_counts=exit_counts,
    )
    return PerformanceMetrics(
        net_return_pct=base.net_return_pct,
        expectancy_r=base.expectancy_r,
        max_drawdown_pct=base.max_drawdown_pct,
        sharpe=base.sharpe,
        profit_factor=base.profit_factor,
        trade_count=base.trade_count,
        consistency_score=base.consistency_score,
        complexity_penalty=base.complexity_penalty,
        gross_fees_usd=base.gross_fees_usd,
        win_rate=base.win_rate,
        exit_reason_counts=base.exit_reason_counts,
        primary_score=_primary_score(base),
        tie_break_score=_tie_break(base, gates),
    )


# ---------- Windowing ------------------------------------------------------


def _as_utc_ts(value: datetime) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _normalize_index(candles: pd.DataFrame) -> pd.DataFrame:
    if candles.empty:
        return candles
    idx = candles.index
    if not isinstance(idx, pd.DatetimeIndex):  # pragma: no cover - defensive
        raise TypeError("candles must have a DatetimeIndex")
    if idx.tz is None:
        candles = candles.copy()
        candles.index = idx.tz_localize("UTC")
    return candles


# ---------- Main entry point ----------------------------------------------


class _Position:
    """The single open position inside the simulation."""

    __slots__ = (
        "entry",
        "entry_fee",
        "mae",
        "mfe",
        "opened_at",
        "opened_index",
        "sl",
        "sl_distance",
        "tp",
        "units",
    )

    def __init__(
        self,
        *,
        entry: float,
        units: float,
        sl: float,
        tp: float,
        sl_distance: float,
        opened_at: datetime,
        opened_index: int,
        entry_fee: float,
    ) -> None:
        self.entry = entry
        self.units = units
        self.sl = sl
        self.tp = tp
        self.sl_distance = sl_distance
        self.opened_at = opened_at
        self.opened_index = opened_index
        self.entry_fee = entry_fee
        self.mfe = 0.0
        self.mae = 0.0


def run(bt: BacktestInput, gates: PromotionGates | None = None) -> BacktestOutput:
    """Run a deterministic backtest. The `now` for risk evaluation is the bar."""
    gates = gates or PROMOTION_GATES
    candles = _normalize_index(bt.candles)
    granularity = bt.config.granularity
    period = granularity_seconds(granularity)

    config_hash = bt.config.canonical_hash_hex()
    input_hash = _input_hash(bt).hex()
    warmup = required_bars(bt.config)

    if candles.empty:
        return BacktestOutput((), EMPTY_METRICS, config_hash, input_hash, bt.oos_label)

    start_ts = _as_utc_ts(bt.start)
    end_ts = _as_utc_ts(bt.end)
    idx = candles.index
    in_window = np.flatnonzero((idx >= start_ts) & (idx <= end_ts))
    if in_window.size == 0:
        return BacktestOutput((), EMPTY_METRICS, config_hash, input_hash, bt.oos_label)

    first_eval = max(int(in_window[0]), warmup)
    last_eval = int(in_window[-1])
    if first_eval > last_eval:
        return BacktestOutput((), EMPTY_METRICS, config_hash, input_hash, bt.oos_label)

    state: AccountState = empty_state(bt.initial_equity)
    trades: list[Trade] = []
    equity_curve: list[float] = [bt.initial_equity]
    period_pnl: dict[str, float] = {}
    position: _Position | None = None
    current_day = idx[first_eval].date()
    current_week = idx[first_eval].isocalendar()[:2]
    # Bar indices of the last entry and the last exit. The discipline gates are
    # all expressed in bars, so these two integers are what age the cooldowns;
    # deriving them from timestamps instead would let a gap in the data expire
    # a cooldown that no bars have actually passed through.
    last_entry_index: int | None = None
    last_exit_index: int | None = None

    highs = candles["h"].to_numpy(dtype=float)
    lows = candles["l"].to_numpy(dtype=float)
    closes = candles["c"].to_numpy(dtype=float)
    opens = candles["o"].to_numpy(dtype=float)
    # Indicators are computed once over the whole series rather than per bar.
    # They are backward-looking, so this is numerically identical to what the
    # live path computes over its own window - and it turns an O(n^2) run into
    # a linear one, which is the difference between the weekly evolution job
    # taking seconds and taking hours.
    indicators = compute_indicators(candles, bt.config)

    for i in range(first_eval, last_eval + 1):
        bar_ts: datetime = idx[i].to_pydatetime()
        if bar_ts.tzinfo is None:
            bar_ts = bar_ts.replace(tzinfo=UTC)

        # --- calendar rollovers -----------------------------------------
        if bar_ts.date() != current_day:
            current_day = bar_ts.date()
            state = roll_day(state)
        week = bar_ts.isocalendar()[:2]
        if week != current_week:
            current_week = week
            state = roll_week(state)

        # --- manage the open position ------------------------------------
        if position is not None:
            position.mfe = max(position.mfe, highs[i] - position.entry)
            position.mae = max(position.mae, position.entry - lows[i])

            exit_reason: ExitReason | None = None
            raw_exit: float | None = None

            # The stop is checked before the target: within a single bar we
            # cannot know which came first, and assuming the good one is how
            # a backtest flatters itself.
            if lows[i] <= position.sl:
                raw_exit = position.sl
                exit_reason = ExitReason.SL
            elif highs[i] >= position.tp:
                raw_exit = position.tp
                exit_reason = ExitReason.TP

            bars_held = i - position.opened_index

            if exit_reason is None and bars_held >= bt.config.time_stop_bars:
                r = (closes[i] - position.entry) / position.sl_distance
                if r < bt.config.time_stop_min_r:
                    raw_exit = closes[i]
                    exit_reason = ExitReason.TIME_STOP

            if exit_reason is None and bars_held >= bt.risk_limits.max_position_hold_bars:
                raw_exit = closes[i]
                exit_reason = ExitReason.HARD_TIME_CAP

            if exit_reason is None and is_weekend(bar_ts, bt.risk_limits):
                raw_exit = closes[i]
                exit_reason = ExitReason.WEEKEND

            if exit_reason is not None and raw_exit is not None:
                fill = _sell_price(raw_exit, bt.slippage_bps)
                exit_fee = _fee(position.units * fill, bt.fee_bps)
                fees = position.entry_fee + exit_fee
                gross = (fill - position.entry) * position.units
                pnl = gross - fees
                risk_usd = position.units * position.sl_distance
                pnl_r = pnl / risk_usd if risk_usd > 0 else 0.0

                trades.append(
                    Trade(
                        opened_at=position.opened_at,
                        closed_at=bar_ts,
                        side=Side.LONG,
                        units=position.units,
                        entry=position.entry,
                        sl=position.sl,
                        tp=position.tp,
                        exit_price=fill,
                        pnl_usd=pnl,
                        pnl_r=pnl_r,
                        fees_usd=fees,
                        mae=position.mae,
                        mfe=position.mfe,
                        mae_r=position.mae / position.sl_distance
                        if position.sl_distance > 0
                        else 0.0,
                        mfe_r=position.mfe / position.sl_distance
                        if position.sl_distance > 0
                        else 0.0,
                        bars_held=max(1, bars_held),
                        exit_reason=exit_reason,
                    )
                )
                state = update_after_close(
                    state,
                    realized_pnl_usd=pnl,
                    close_ts=bar_ts,
                    side=Side.LONG,
                    exit_reason=exit_reason,
                    bars_held=max(1, bars_held),
                )
                equity_curve.append(state.equity)
                key = f"{bar_ts.isocalendar()[0]}-W{bar_ts.isocalendar()[1]:02d}"
                period_pnl[key] = period_pnl.get(key, 0.0) + pnl
                position = None
                last_exit_index = i

        # --- age the discipline counters ---------------------------------
        state = state.model_copy(
            update={
                "is_weekend": is_weekend(bar_ts, bt.risk_limits),
                "bars_since_last_exit": (
                    None if last_exit_index is None else i - last_exit_index
                ),
                "bars_since_last_entry": (
                    None if last_entry_index is None else i - last_entry_index
                ),
            }
        )

        # --- consider a new entry ----------------------------------------
        if position is None:
            prop = evaluate_bar(
                candles,
                indicators,
                i,
                bt.config,
                equity=state.equity,
                strategy_version_id=0,
                highs=highs,
                lows=lows,
                closes=closes,
            )
            if prop is not None:
                market = MarketState(
                    instrument=prop.instrument,
                    granularity=granularity,
                    last_bar_ts=bar_ts,
                    last_close=float(closes[i]),
                    indicators=prop.indicators,
                    seconds_since_last_bar=0,
                )
                decision = evaluate(prop, state, bt.risk_limits, market, bar_ts)
                units = (
                    floor_to_lot(decision.order.units, SIM.min_lot_step)
                    if decision.approved and decision.order is not None
                    else 0.0
                )
                # The signal is the close of bar i; the fill is the open of
                # bar i+1, because that is the first price actually tradeable
                # after the bar closed. Filling at the signal close is the
                # oldest way to make a backtest look better than reality.
                if units > 0.0 and decision.order is not None and i + 1 <= last_eval:
                    fill = _buy_price(float(opens[i + 1]), bt.slippage_bps)
                    entry_fee = _fee(units * fill, bt.fee_bps)
                    sl_distance = decision.order.sl_distance
                    position = _Position(
                        entry=fill,
                        units=units,
                        sl=fill - sl_distance,
                        tp=fill + bt.config.tp_r_multiple * sl_distance,
                        sl_distance=sl_distance,
                        opened_at=bar_ts + timedelta(seconds=period),
                        opened_index=i + 1,
                        entry_fee=entry_fee,
                    )
                    last_entry_index = i + 1
                    state = state.model_copy(
                        update={
                            "open_positions": 1,
                            "trades_today": state.trades_today + 1,
                            "bars_since_last_entry": 0,
                        }
                    )

    # --- close anything still open at the end of the window ---------------
    if position is not None:
        fill = _sell_price(float(closes[last_eval]), bt.slippage_bps)
        exit_fee = _fee(position.units * fill, bt.fee_bps)
        fees = position.entry_fee + exit_fee
        pnl = (fill - position.entry) * position.units - fees
        risk_usd = position.units * position.sl_distance
        bar_ts = idx[last_eval].to_pydatetime()
        if bar_ts.tzinfo is None:
            bar_ts = bar_ts.replace(tzinfo=UTC)
        bars_held = max(1, last_eval - position.opened_index)
        trades.append(
            Trade(
                opened_at=position.opened_at,
                closed_at=bar_ts,
                side=Side.LONG,
                units=position.units,
                entry=position.entry,
                sl=position.sl,
                tp=position.tp,
                exit_price=fill,
                pnl_usd=pnl,
                pnl_r=pnl / risk_usd if risk_usd > 0 else 0.0,
                fees_usd=fees,
                mae=position.mae,
                mfe=position.mfe,
                mae_r=position.mae / position.sl_distance if position.sl_distance > 0 else 0.0,
                mfe_r=position.mfe / position.sl_distance if position.sl_distance > 0 else 0.0,
                bars_held=bars_held,
                exit_reason=ExitReason.RECONCILED,
            )
        )
        state = update_after_close(
            state,
            realized_pnl_usd=pnl,
            close_ts=bar_ts,
            side=Side.LONG,
            exit_reason=ExitReason.RECONCILED,
            bars_held=bars_held,
        )
        equity_curve.append(state.equity)
        key = f"{bar_ts.isocalendar()[0]}-W{bar_ts.isocalendar()[1]:02d}"
        period_pnl[key] = period_pnl.get(key, 0.0) + pnl

    num_params = len(
        [f for f in type(bt.config).model_fields if bt.config.is_field_mutable_by_llm(f)]
    )
    metrics = compute_metrics(
        tuple(trades),
        initial_equity=bt.initial_equity,
        final_equity=state.equity,
        equity_curve=equity_curve,
        period_pnl=period_pnl,
        num_params=num_params,
        gates=gates,
    )
    return BacktestOutput(tuple(trades), metrics, config_hash, input_hash, bt.oos_label)


def _input_hash(bt: BacktestInput) -> bytes:
    h = hashlib.sha256()
    h.update(bt.config.canonical_hash())
    if not bt.candles.empty:
        h.update(pd.util.hash_pandas_object(bt.candles, index=True).values.tobytes())
    h.update(bt.start.isoformat().encode())
    h.update(bt.end.isoformat().encode())
    h.update(f"{bt.initial_equity}|{bt.fee_bps}|{bt.slippage_bps}".encode())
    h.update(bt.session_id.encode())
    return h.digest()
