"""The deterministic Backtester.

This is the single source of truth for "what would have happened". The
backtester and the live StrategyRunner share the same propose function;
the backtester adds the matching engine, the PnL accounting, and the
metrics. The backtester is a **pure function** of its input.

A 10-run determinism test on the same input produces byte-identical
output. This is enforced by `tests/test_backtester.py`.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import numpy as np
import pandas as pd

from app.risk.engine import evaluate
from app.risk.types import AccountState, MarketState, OrderType, Side
from app.strategy.configs import StrategyConfig
from app.strategy.runner import propose
from pinned.news_policy import NewsPolicy
from pinned.risk_limits import RISK_LIMITS, RiskLimits
from pinned.simulation_constants import SIM


# ---------- Types ----------------------------------------------------------


@dataclass(frozen=True)
class Trade:
    opened_at: datetime
    closed_at: datetime
    side: Side
    units: int
    entry: float
    sl: float
    tp: float
    exit_price: float
    pnl_usd: float
    pnl_r: float
    mae: float                                  # max adverse excursion in price units
    mfe: float                                  # max favorable excursion in price units
    exit_reason: Literal["sl", "tp", "time_stop", "weekend", "eod"]


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
    news_policy: NewsPolicy = NewsPolicy.NORMAL
    oos_label: Literal["train", "validation", "holdout"] = "train"
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
    oos_score: float
    complexity_penalty: float
    regime_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)
    primary_score: float = 0.0
    tie_break_score: float = 0.0


@dataclass(frozen=True)
class BacktestOutput:
    trades: tuple[Trade, ...]
    metrics: PerformanceMetrics
    config_hash: str
    input_hash: str
    oos_label: str


# ---------- The matching engine -------------------------------------------


def _round_to_units(units: int) -> int:
    return (units // SIM.position_round_units) * SIM.position_round_units


def _apply_slippage(price: float, side: Side, slippage_bps: float) -> float:
    if side == Side.LONG:
        return price * (1.0 + slippage_bps / 10_000.0)
    return price * (1.0 - slippage_bps / 10_000.0)


def _apply_fee(notional: float, fee_bps: float) -> float:
    return notional * (fee_bps / 10_000.0)


def _initial_metrics(trades: tuple[Trade, ...], params: int, oos_score: float) -> PerformanceMetrics:
    if not trades:
        return PerformanceMetrics(
            net_return_pct=0.0,
            expectancy_r=0.0,
            max_drawdown_pct=0.0,
            sharpe=0.0,
            profit_factor=0.0,
            trade_count=0,
            consistency_score=0.0,
            oos_score=oos_score,
            complexity_penalty=0.005 * params,
            primary_score=0.0,
            tie_break_score=0.0,
        )
    pnls = np.array([t.pnl_r for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    expectancy = float(pnls.mean())
    win_sum = float(wins.sum()) if wins.size else 0.0
    loss_sum = float(-losses.sum()) if losses.size else 0.0
    profit_factor = win_sum / loss_sum if loss_sum > 0 else math.inf
    sharpe = float(pnls.mean() / pnls.std(ddof=1) * math.sqrt(len(pnls))) if pnls.std(ddof=1) > 0 else 0.0
    # Net return from pnl_usd
    pnl_usd = float(sum(t.pnl_usd for t in trades))
    return PerformanceMetrics(
        net_return_pct=pnl_usd,                  # computed by caller with initial equity
        expectancy_r=expectancy,
        max_drawdown_pct=0.0,                    # computed by caller
        sharpe=sharpe,
        profit_factor=profit_factor,
        trade_count=len(trades),
        consistency_score=0.0,                   # computed by caller
        oos_score=oos_score,
        complexity_penalty=0.005 * params,
        primary_score=0.0,
        tie_break_score=0.0,
    )


def _max_drawdown(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        if e > peak:
            peak = e
        dd = (peak - e) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _consistency_score(monthly_pnl: dict[tuple[int, int], float]) -> float:
    if not monthly_pnl:
        return 0.0
    pos = sum(1 for v in monthly_pnl.values() if v > 0)
    return pos / len(monthly_pnl)


def _primary_score(m: PerformanceMetrics) -> float:
    if m.trade_count == 0:
        return 0.0
    if m.max_drawdown_pct <= 0:
        return 0.0
    return (
        m.expectancy_r
        * m.consistency_score
        / m.max_drawdown_pct
        * (1.0 - m.complexity_penalty)
    )


def _tie_break(m: PerformanceMetrics, gates) -> float:
    return gates.score_sharpe_weight * m.sharpe + gates.score_profit_factor_weight * m.profit_factor


# ---------- Main entry point ----------------------------------------------


def _windowed_candles(candles: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    if candles.empty:
        return candles
    if candles.index.tz is None:
        candles = candles.copy()
        candles.index = candles.index.tz_localize("UTC")
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    else:
        start_ts = start_ts.tz_convert("UTC")
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    else:
        end_ts = end_ts.tz_convert("UTC")
    mask = (candles.index >= start_ts) & (candles.index <= end_ts)
    return candles[mask]


def run(
    bt: BacktestInput,
    gates=None,  # type: ignore[no-untyped-def]
) -> BacktestOutput:
    """Run a deterministic backtest on the given input.

    Pure function: no I/O, no datetime.now() inside. The `now` for risk
    evaluation is the bar's own timestamp.
    """
    from pinned.promotion_gates import PROMOTION_GATES
    gates = gates or PROMOTION_GATES

    candles = _windowed_candles(bt.candles, bt.start, bt.end)
    if candles.empty or len(candles) < 60:
        # Need enough bars to seed the slow EMA
        empty_metrics = PerformanceMetrics(
            net_return_pct=0.0,
            expectancy_r=0.0,
            max_drawdown_pct=0.0,
            sharpe=0.0,
            profit_factor=0.0,
            trade_count=0,
            consistency_score=0.0,
            oos_score=0.0,
            complexity_penalty=0.005 * 6,
            primary_score=0.0,
            tie_break_score=0.0,
        )
        return BacktestOutput(
            trades=(),
            metrics=empty_metrics,
            config_hash=bt.config.canonical_hash().hex(),
            input_hash=_input_hash(bt).hex(),
            oos_label=bt.oos_label,
        )

    state = AccountState(
        equity=bt.initial_equity,
        equity_high=bt.initial_equity,
        day_pnl=0.0,
        week_pnl=0.0,
        drawdown_pct=0.0,
        open_positions=0,
        consecutive_losses=0,
        is_kill_switch=False,
    )
    news_policy = bt.news_policy

    trades: list[Trade] = []
    equity_curve: list[float] = [bt.initial_equity]
    monthly_pnl: dict[tuple[int, int], float] = {}
    open_trade: Trade | None = None
    open_units = 0
    open_sl: float | None = None
    open_tp: float | None = None
    open_side: Side | None = None
    open_entry: float = 0.0
    open_opened_at: datetime | None = None
    open_sl_dist: float = 0.0
    mfe = 0.0
    mae = 0.0

    # Iterate bar by bar
    for i in range(50, len(candles)):
        bar = candles.iloc[i]
        prev = candles.iloc[i - 1]
        bar_ts = candles.index[i]
        if bar_ts.tzinfo is None:
            bar_ts_dt = bar_ts.to_pydatetime().replace(tzinfo=__import__("datetime").timezone.utc)
        else:
            bar_ts_dt = bar_ts.to_pydatetime()
        # ---- Manage open trade (if any) ----
        if open_trade is not None and open_side is not None:
            # Update MFE/MAE from high/low
            if open_side == Side.LONG:
                mfe = max(mfe, float(bar["h"]) - open_entry)
                mae = max(mae, open_entry - float(bar["l"]))
            else:
                mfe = max(mfe, open_entry - float(bar["l"]))
                mfe = max(mfe, open_entry - float(bar["l"]))
                mae = max(mae, float(bar["h"]) - open_entry)
            # Check SL / TP hits
            exit_reason: str | None = None
            exit_price: float | None = None
            if open_sl is not None and open_tp is not None:
                if open_side == Side.LONG:
                    if float(bar["l"]) <= open_sl:
                        exit_price = open_sl
                        exit_reason = "sl"
                    elif float(bar["h"]) >= open_tp:
                        exit_price = open_tp
                        exit_reason = "tp"
                else:
                    if float(bar["h"]) >= open_sl:
                        exit_price = open_sl
                        exit_reason = "sl"
                    elif float(bar["l"]) <= open_tp:
                        exit_price = open_tp
                        exit_reason = "tp"
            # Time stop
            if exit_reason is None and open_opened_at is not None:
                hours_open = (bar_ts_dt - open_opened_at).total_seconds() / 3600.0
                if hours_open >= bt.config.time_stop_hours:
                    # R = (current_price - entry) / sl_distance for long
                    cur = float(bar["c"])
                    r = (cur - open_entry) / open_sl_dist if open_sl_dist > 0 else 0.0
                    if open_side == Side.SHORT:
                        r = -r
                    if r < bt.config.time_stop_min_r:
                        exit_price = cur
                        exit_reason = "time_stop"
            # Weekend flatten
            if exit_reason is None and bar_ts_dt.weekday() == 4 and bar_ts_dt.hour >= 20:
                exit_price = float(bar["c"])
                exit_reason = "weekend"

            if exit_reason is not None and exit_price is not None:
                exit_price = _apply_slippage(exit_price, open_side, bt.slippage_bps)
                pnl = _compute_pnl(open_entry, exit_price, open_units, open_side)
                fee = _apply_fee(abs(open_units * exit_price), bt.fee_bps)
                pnl_usd = pnl - fee
                pnl_r = pnl / (open_units * open_sl_dist) if open_sl_dist > 0 else 0.0
                t = Trade(
                    opened_at=open_opened_at,
                    closed_at=bar_ts_dt,
                    side=open_side,
                    units=open_units,
                    entry=open_entry,
                    sl=open_sl if open_sl is not None else 0.0,
                    tp=open_tp if open_tp is not None else 0.0,
                    exit_price=exit_price,
                    pnl_usd=pnl_usd,
                    pnl_r=pnl_r,
                    mae=mae,
                    mfe=mfe,
                    exit_reason=exit_reason,  # type: ignore[arg-type]
                )
                trades.append(t)
                state.equity += pnl_usd
                state.equity_high = max(state.equity_high, state.equity)
                state.day_pnl += pnl_usd
                state.week_pnl += pnl_usd
                state.consecutive_losses = state.consecutive_losses + 1 if pnl_usd < 0 else 0
                state.drawdown_pct = (
                    (state.equity_high - state.equity) / state.equity_high if state.equity_high > 0 else 0.0
                )
                state.open_positions = 0
                state.last_trade_close_ts = bar_ts_dt
                equity_curve.append(state.equity)
                ym = (bar_ts_dt.year, bar_ts_dt.month)
                monthly_pnl[ym] = monthly_pnl.get(ym, 0.0) + pnl_usd
                open_trade = None
                open_units = 0
                open_sl = open_tp = None
                open_side = None
                open_entry = 0.0
                open_opened_at = None
                open_sl_dist = 0.0
                mfe = mae = 0.0

        # ---- Maybe open a new trade ----
        if open_trade is None and state.open_positions == 0:
            window = candles.iloc[: i + 1]
            proposals = propose(
                window,
                window.iloc[-1],
                bt.config,
                equity=state.equity,
                strategy_version_id=0,           # 0 in backtest (no DB)
                session_id=bt.session_id,
            )
            for prop in proposals:
                market = MarketState(
                    instrument=prop.instrument,
                    granularity=bt.config.granularity,
                    last_bar_ts=bar_ts_dt,
                    last_close=float(bar["c"]),
                    seconds_since_last_bar=0,
                )
                decision = evaluate(prop, state, bt.risk_limits, news_policy, market, bar_ts_dt)
                if decision.decision == "approved" and decision.order is not None:
                    open_side = decision.order.side
                    open_units = _round_to_units(decision.order.units)
                    if open_units <= 0:
                        continue
                    open_entry = _apply_slippage(decision.order.entry_price, open_side, bt.slippage_bps)
                    open_sl = decision.order.stop_loss
                    open_tp = decision.order.take_profit
                    open_opened_at = bar_ts_dt
                    open_sl_dist = abs(open_entry - open_sl) if open_sl is not None else 0.0
                    mfe = mae = 0.0
                    open_trade = prop
                    state.open_positions = 1
                    break

    # ---- Compute metrics ----
    num_params = sum(1 for f in type(bt.config).model_fields if bt.config.is_field_mutable_by_llm(f))
    base = _initial_metrics(tuple(trades), num_params, oos_score=0.0)
    final_equity = state.equity
    net_return = (final_equity - bt.initial_equity) / bt.initial_equity
    max_dd = _max_drawdown(equity_curve)
    consistency = _consistency_score(monthly_pnl)
    metrics = PerformanceMetrics(
        net_return_pct=net_return,
        expectancy_r=base.expectancy_r,
        max_drawdown_pct=max_dd,
        sharpe=base.sharpe,
        profit_factor=base.profit_factor,
        trade_count=base.trade_count,
        consistency_score=consistency,
        oos_score=0.0,
        complexity_penalty=base.complexity_penalty,
    )
    primary = _primary_score(metrics)
    tie = _tie_break(metrics, gates)
    metrics = PerformanceMetrics(
        net_return_pct=metrics.net_return_pct,
        expectancy_r=metrics.expectancy_r,
        max_drawdown_pct=metrics.max_drawdown_pct,
        sharpe=metrics.sharpe,
        profit_factor=metrics.profit_factor,
        trade_count=metrics.trade_count,
        consistency_score=metrics.consistency_score,
        oos_score=metrics.oos_score,
        complexity_penalty=metrics.complexity_penalty,
        primary_score=primary,
        tie_break_score=tie,
    )

    return BacktestOutput(
        trades=tuple(trades),
        metrics=metrics,
        config_hash=bt.config.canonical_hash().hex(),
        input_hash=_input_hash(bt).hex(),
        oos_label=bt.oos_label,
    )


def _compute_pnl(entry: float, exit_: float, units: int, side: Side) -> float:
    if side == Side.LONG:
        return (exit_ - entry) * units
    return (entry - exit_) * units


def _input_hash(bt: BacktestInput) -> bytes:
    h = hashlib.sha256()
    h.update(bt.config.canonical_hash())
    h.update(bt.candles.to_json().encode("utf-8") if not bt.candles.empty else b"")
    h.update(bt.start.isoformat().encode())
    h.update(bt.end.isoformat().encode())
    h.update(str(bt.initial_equity).encode())
    h.update(bt.session_id.encode())
    return h.digest()
