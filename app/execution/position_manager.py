"""Position lifecycle: open, protect, monitor, exit, reconcile.

This module exists because the previous system had none. It computed a stop
loss, validated it, and then dropped it on the floor: the order that reached
Alpaca was a bare market order with no stop, no target, and no code anywhere
that would ever close the position. A crash left an unbounded position open
with nothing at the broker to stop it.

The design here is built around one rule: **a position without a
broker-resident stop is an emergency**. It may exist for at most the gap
between opening it and the next line of code; if the stop cannot be placed,
the position is flattened immediately rather than left to hope.

State machine (persisted in `trades.status`, so it survives a restart):

    PENDING_ENTRY -> OPEN_UNPROTECTED -> OPEN_PROTECTED -> PENDING_EXIT -> CLOSED

Alpaca crypto has no plain `stop` order type, so the protective order is a
`stop_limit` whose limit sits `SIM.stop_limit_band_bps` below the trigger:
wide enough that a fast move still fills, rather than leaving the position
naked at the exact moment protection matters. A second, independent failsafe
in `monitor()` market-sells if price trades through the stop and the stop
order somehow did not fill.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

import pandas as pd

from app.broker.types import (
    Broker,
    BrokerError,
    BrokerOrderType,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
)
from app.db import trades as trades_db
from app.db.trades import TradeRecord, TradeStatus
from app.journal.payloads import (
    RiskEventPayload,
    SystemEventPayload,
    TradeClosedPayload,
    TradeOpenedPayload,
)
from app.journal.types import JournalKind, JournalState
from app.journal.writer import JournalWriter
from app.market.bars import bars_between
from app.risk.types import ExitReason, OrderSpec, ReasonCode, Side
from app.strategy.configs import StrategyConfig
from pinned.risk_limits import RISK_LIMITS, RiskLimits
from pinned.simulation_constants import SIM

from .excursions import compute_mfe_mae
from .tags import exit_tag, stop_tag

logger = logging.getLogger(__name__)

_ENTRY_POLL_ATTEMPTS = 10
_ENTRY_POLL_INTERVAL_SEC = 1.0


class ExitMode(StrEnum):
    """How protective exits are arranged at the broker.

    `STOP_LIMIT_PLUS_CLIENT_TP` is what Alpaca crypto actually allows: a
    resting stop_limit for the downside, with the target managed here.
    `cli broker-probe --probe-bracket` was run against a live paper account
    and the answer was explicit — "crypto orders not allowed for advanced
    order_class: otoco" — so `BRACKET` exists only for a broker that does
    support it, and is never selected without a probe result saying so.
    """

    STOP_LIMIT_PLUS_CLIENT_TP = "stop_limit_plus_client_tp"
    BRACKET = "bracket"


@dataclass
class ReconcileReport:
    adopted: list[int] = field(default_factory=list)
    closed_from_broker: list[int] = field(default_factory=list)
    orphans_flattened: int = 0
    stray_orders_cancelled: int = 0
    stops_replaced: list[int] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (
            self.adopted
            or self.closed_from_broker
            or self.orphans_flattened
            or self.stray_orders_cancelled
            or self.stops_replaced
        )


def _bps(actual: float, reference: float) -> float:
    if reference <= 0:
        return 0.0
    return (actual - reference) / reference * 10_000.0


def _now() -> datetime:
    return datetime.now(tz=UTC)


class PositionManager:
    def __init__(
        self,
        *,
        broker: Broker,
        conn: sqlite3.Connection,
        journal: JournalWriter,
        instrument: str,
        granularity: str,
        limits: RiskLimits = RISK_LIMITS,
        exit_mode: ExitMode = ExitMode.STOP_LIMIT_PLUS_CLIENT_TP,
        poll_attempts: int = _ENTRY_POLL_ATTEMPTS,
        poll_interval_sec: float = _ENTRY_POLL_INTERVAL_SEC,
    ) -> None:
        self._broker = broker
        self._conn = conn
        self._journal = journal
        self._instrument = instrument
        self._granularity = granularity
        self._limits = limits
        self._exit_mode = exit_mode
        self._poll_attempts = poll_attempts
        self._poll_interval = poll_interval_sec
        # Trades whose price has traded through the stop while the stop order
        # was still working. Two consecutive sightings trigger the failsafe;
        # one could be a stale quote.
        self._through_stop_strikes: dict[int, int] = {}

    # ------------------------------------------------------------------ open

    def open(
        self,
        order: OrderSpec,
        *,
        config: StrategyConfig,
        decision_close: float,
        now: datetime | None = None,
    ) -> TradeRecord | None:
        """Enter the position and protect it, or leave nothing behind."""
        moment = now or _now()
        if order.side is not Side.LONG:
            raise ValueError("only long entries are supported (crypto cannot be shorted)")

        entry_order = self._submit_entry(order)
        if entry_order is None:
            return None

        filled_qty = entry_order.filled_qty
        fill_price = entry_order.filled_avg_price
        if filled_qty <= 0 or fill_price is None:
            self._journal.write(
                kind=JournalKind.TRADE_DECISION,
                payload=SystemEventPayload(
                    event="entry_unfilled",
                    detail=f"order {entry_order.id} status={entry_order.status.value}",
                    data={"client_tag": order.client_tag},
                ),
                strategy_version_id=order.strategy_version_id,
                prev_state=JournalState.ORDER_SENT,
                next_state=JournalState.ORDER_REJECTED,
            )
            return None

        # Recompute the protective levels from the ACTUAL fill, keeping the
        # planned distance. Sizing was chosen for that distance; anchoring the
        # stop to the intended price instead would silently change the risk.
        sl_distance = order.sl_distance
        sl_price = fill_price - sl_distance
        tp_price = fill_price + config.tp_r_multiple * sl_distance
        slippage_bps = _bps(fill_price, decision_close)

        trade_id = trades_db.insert_open(
            self._conn,
            client_tag=order.client_tag,
            strategy_version_id=order.strategy_version_id,
            config_hash=config.canonical_hash_hex(),
            session_id=self._journal.session_id,
            status=TradeStatus.OPEN_UNPROTECTED,
            side=Side.LONG,
            units=filled_qty,
            signal_bar_ts=order.bar_ts,
            decision_close=decision_close,
            opened_at=entry_order.filled_at or moment,
            entry_order_id=entry_order.id,
            entry_fill_price=fill_price,
            entry_slippage_bps=slippage_bps,
            sl=sl_price,
            tp=tp_price,
            sl_distance=sl_distance,
            sl_order_id=None,
            indicators=order.indicators,
        )

        if abs(slippage_bps) > self._limits.max_entry_slippage_bps:
            self._journal.write(
                kind=JournalKind.RISK_EVENT,
                payload=RiskEventPayload(
                    event=ReasonCode.ENTRY_SLIPPAGE_EXCEEDED,
                    detail=(
                        f"filled at {fill_price:.2f} vs decision close "
                        f"{decision_close:.2f} ({slippage_bps:+.1f} bps)"
                    ),
                    trade_id=trade_id,
                    client_tag=order.client_tag,
                    value=slippage_bps,
                    action="journaled; position still protected normally",
                ),
                strategy_version_id=order.strategy_version_id,
                reason_codes=[ReasonCode.ENTRY_SLIPPAGE_EXCEEDED.value],
            )

        stop_order = self._place_stop(
            trade_id=trade_id,
            client_tag=order.client_tag,
            qty=filled_qty,
            stop_price=sl_price,
            attempt=0,
        )

        if stop_order is None:
            # Unprotected. This is the one condition the whole module exists
            # to prevent lasting longer than a few hundred milliseconds.
            self._flatten_unprotected(trade_id=trade_id, qty=filled_qty, now=moment)
            return trades_db.get(self._conn, trade_id)

        trades_db.set_status(
            self._conn,
            trade_id,
            TradeStatus.OPEN_PROTECTED,
            sl_order_id=stop_order.id,
        )

        entry = self._journal.write(
            kind=JournalKind.TRADE_OPENED,
            payload=TradeOpenedPayload(
                trade_id=trade_id,
                client_tag=order.client_tag,
                bar_ts=order.bar_ts,
                side=Side.LONG,
                units=filled_qty,
                decision_close=decision_close,
                entry_fill_price=fill_price,
                entry_slippage_bps=slippage_bps,
                stop_loss=sl_price,
                take_profit=tp_price,
                sl_distance=sl_distance,
                entry_order_id=entry_order.id,
                sl_order_id=stop_order.id,
                protected=True,
                config_hash=config.canonical_hash_hex(),
                indicators=order.indicators,
            ),
            strategy_version_id=order.strategy_version_id,
            prev_state=JournalState.ORDER_SENT,
            next_state=JournalState.OPEN_PROTECTED,
        )
        if entry.id is not None:
            trades_db.set_journal_open_id(self._conn, trade_id, entry.id)

        logger.info(
            "opened %s %.4f @ %.2f (sl %.2f, tp %.2f, trade %d)",
            self._instrument,
            filled_qty,
            fill_price,
            sl_price,
            tp_price,
            trade_id,
        )
        return trades_db.get(self._conn, trade_id)

    def _submit_entry(self, order: OrderSpec) -> Order | None:
        req = OrderRequest(
            symbol=order.instrument,
            qty=order.units,
            side=OrderSide.BUY,
            type=BrokerOrderType.MARKET,
            time_in_force=SIM.entry_tif,
            client_order_id=order.client_tag,
        )
        try:
            submitted = self._broker.submit_order(req)
        except BrokerError as exc:
            logger.error("entry order rejected: %s", exc)
            self._journal.write(
                kind=JournalKind.RISK_EVENT,
                payload=RiskEventPayload(
                    event=ReasonCode.INVALID_PROPOSAL,
                    detail=f"entry submission failed: {exc}",
                    client_tag=order.client_tag,
                    action="no position opened",
                ),
                strategy_version_id=order.strategy_version_id,
            )
            return None
        return self._await_fill(submitted)

    def _await_fill(self, order: Order) -> Order:
        current = order
        for _ in range(self._poll_attempts):
            if current.status.is_terminal and current.filled_qty > 0:
                return current
            if current.status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
                return current
            if current.status is OrderStatus.EXPIRED:
                return current
            time.sleep(self._poll_interval)
            try:
                current = self._broker.get_order(order.id)
            except BrokerError:
                return current
        return current

    def _place_stop(
        self,
        *,
        trade_id: int,
        client_tag: str,
        qty: float,
        stop_price: float,
        attempt: int,
    ) -> Order | None:
        limit_price = stop_price * (1.0 - SIM.stop_limit_band_bps / 10_000.0)
        req = OrderRequest(
            symbol=self._instrument,
            qty=qty,
            side=OrderSide.SELL,
            type=BrokerOrderType.STOP_LIMIT,
            time_in_force="gtc",
            client_order_id=stop_tag(client_tag, attempt=attempt),
            stop_price=round(stop_price, 2),
            limit_price=round(limit_price, 2),
        )
        try:
            return self._broker.submit_order(req)
        except (BrokerError, PermissionError) as exc:
            logger.error("protective stop placement failed for trade %d: %s", trade_id, exc)
            return None

    def _flatten_unprotected(self, *, trade_id: int, qty: float, now: datetime) -> None:
        self._journal.write(
            kind=JournalKind.RISK_EVENT,
            payload=RiskEventPayload(
                event=ReasonCode.UNPROTECTED_POSITION_FLATTENED,
                detail="protective stop could not be placed; flattening immediately",
                trade_id=trade_id,
                value=qty,
                action="market sell IOC",
            ),
            reason_codes=[ReasonCode.UNPROTECTED_POSITION_FLATTENED.value],
        )
        trade = trades_db.get(self._conn, trade_id)
        if trade is None:  # pragma: no cover - just written
            return
        self._exit_now(trade, reason=ExitReason.UNPROTECTED, now=now)

    # --------------------------------------------------------------- monitor

    def monitor(
        self,
        *,
        now: datetime | None = None,
        latest_bid: float | None = None,
        closed_bars: pd.DataFrame | None = None,
        kill_switch: bool = False,
    ) -> list[TradeRecord]:
        """Advance every open trade one cycle. Returns the trades closed."""
        moment = now or _now()
        closed: list[TradeRecord] = []

        for trade in trades_db.open_trades(self._conn):
            result = self._monitor_one(
                trade,
                now=moment,
                latest_bid=latest_bid,
                closed_bars=closed_bars,
                kill_switch=kill_switch,
            )
            if result is not None:
                closed.append(result)
        return closed

    def _monitor_one(
        self,
        trade: TradeRecord,
        *,
        now: datetime,
        latest_bid: float | None,
        closed_bars: pd.DataFrame | None,
        kill_switch: bool,
    ) -> TradeRecord | None:
        # (a) Did the protective stop fill, or did the position vanish?
        stop_order = self._get_stop_order(trade)
        if stop_order is not None and stop_order.status is OrderStatus.FILLED:
            return self._close_from_order(
                trade, stop_order, reason=ExitReason.SL, closed_bars=closed_bars, now=now
            )

        position_qty = self._position_qty()
        if position_qty <= 0.0:
            # The broker says flat. Find the fill that closed it rather than
            # guessing a price.
            return self._close_from_broker_history(trade, closed_bars=closed_bars, now=now)

        # (e) Kill switch outranks every discretionary exit below.
        if kill_switch and self._limits.flatten_open_position_on_kill_switch:
            return self._exit_now(
                trade, reason=ExitReason.KILL_SWITCH, now=now, closed_bars=closed_bars
            )

        # (g) Protected? If the stop is gone but the position is not, re-place
        #     it before considering anything else.
        if stop_order is None or not stop_order.status.is_working:
            replaced = self._replace_stop(trade, qty=position_qty, now=now)
            if replaced is None:
                return self._exit_now(
                    trade,
                    reason=ExitReason.UNPROTECTED,
                    now=now,
                    closed_bars=closed_bars,
                )
            stop_order = replaced

        bid = latest_bid if latest_bid is not None else self._safe_bid()

        # (f) Failsafe: price is through the stop but the stop has not filled.
        if bid is not None and bid < trade.sl:
            strikes = self._through_stop_strikes.get(trade.id, 0) + 1
            self._through_stop_strikes[trade.id] = strikes
            if strikes >= 2:
                self._through_stop_strikes.pop(trade.id, None)
                return self._exit_now(
                    trade,
                    reason=ExitReason.SL_FAILSAFE,
                    now=now,
                    closed_bars=closed_bars,
                )
        else:
            self._through_stop_strikes.pop(trade.id, None)

        # (b) Target.
        if bid is not None and bid >= trade.tp:
            return self._exit_now(
                trade, reason=ExitReason.TP, now=now, closed_bars=closed_bars
            )

        bars_held = bars_between(trade.opened_at, now, self._granularity)

        # (d) Hard cap on position lifetime, regardless of P&L.
        if bars_held >= self._limits.max_position_hold_bars:
            return self._exit_now(
                trade, reason=ExitReason.HARD_TIME_CAP, now=now, closed_bars=closed_bars
            )

        # (c) Time stop: the idea had its chance and did not work.
        config = self._config_for(trade)
        if config is not None and bars_held >= config.time_stop_bars:
            entry = trade.entry_fill_price or trade.decision_close
            price = bid if bid is not None else entry
            r = (price - entry) / trade.sl_distance if trade.sl_distance > 0 else 0.0
            if r < config.time_stop_min_r:
                return self._exit_now(
                    trade, reason=ExitReason.TIME_STOP, now=now, closed_bars=closed_bars
                )

        return None

    # ----------------------------------------------------------- exit paths

    def _exit_now(
        self,
        trade: TradeRecord,
        *,
        reason: ExitReason,
        now: datetime,
        closed_bars: pd.DataFrame | None = None,
    ) -> TradeRecord | None:
        """Cancel the resting stop and market-sell the position."""
        trades_db.set_status(self._conn, trade.id, TradeStatus.PENDING_EXIT)

        if trade.sl_order_id:
            try:
                self._broker.cancel_order(trade.sl_order_id)
            except (BrokerError, PermissionError) as exc:
                logger.warning("could not cancel stop %s: %s", trade.sl_order_id, exc)

        qty = self._position_qty() or trade.units
        if qty <= 0:
            return self._close_from_broker_history(trade, closed_bars=closed_bars, now=now)

        req = OrderRequest(
            symbol=self._instrument,
            qty=qty,
            side=OrderSide.SELL,
            type=BrokerOrderType.MARKET,
            time_in_force=SIM.entry_tif,
            client_order_id=exit_tag(trade.client_tag, reason=reason.value),
        )
        try:
            order = self._await_fill(self._broker.submit_order(req))
        except (BrokerError, PermissionError) as exc:
            logger.error("exit order failed for trade %d: %s", trade.id, exc)
            self._journal.write(
                kind=JournalKind.RISK_EVENT,
                payload=RiskEventPayload(
                    event=ReasonCode.UNPROTECTED_POSITION_FLATTENED,
                    detail=f"exit submission failed: {exc}",
                    trade_id=trade.id,
                    client_tag=trade.client_tag,
                    action="will retry on the next monitor cycle",
                ),
            )
            trades_db.set_status(self._conn, trade.id, TradeStatus.OPEN_UNPROTECTED)
            return None

        return self._close_from_order(
            trade, order, reason=reason, closed_bars=closed_bars, now=now
        )

    def _close_from_order(
        self,
        trade: TradeRecord,
        order: Order,
        *,
        reason: ExitReason,
        closed_bars: pd.DataFrame | None,
        now: datetime,
    ) -> TradeRecord | None:
        exit_price = order.filled_avg_price
        if exit_price is None or order.filled_qty <= 0:
            logger.warning("exit order %s did not fill; leaving trade open", order.id)
            trades_db.set_status(self._conn, trade.id, TradeStatus.OPEN_UNPROTECTED)
            return None
        return self._finalize(
            trade,
            exit_price=exit_price,
            exit_order_id=order.id,
            closed_at=order.filled_at or now,
            reason=reason,
            closed_bars=closed_bars,
        )

    def _close_from_broker_history(
        self,
        trade: TradeRecord,
        *,
        closed_bars: pd.DataFrame | None,
        now: datetime,
    ) -> TradeRecord | None:
        """The position is gone; find out at what price and record it."""
        fill_price: float | None = None
        fill_id: str | None = None
        fill_at: datetime | None = None
        reason = ExitReason.RECONCILED

        try:
            recent = self._broker.list_orders(status="closed", after=trade.opened_at, limit=50)
        except BrokerError:
            recent = []

        for o in recent:
            if o.side is not OrderSide.SELL or o.filled_qty <= 0:
                continue
            if not o.client_order_id.startswith(trade.client_tag):
                continue
            fill_price = o.filled_avg_price
            fill_id = o.id
            fill_at = o.filled_at
            reason = (
                ExitReason.SL
                if o.type is BrokerOrderType.STOP_LIMIT
                else ExitReason.RECONCILED
            )
            break

        if fill_price is None:
            # Nothing attributable. Use the last known price so the trade is
            # still closed with an honest estimate rather than left dangling.
            fill_price = self._safe_bid() or trade.entry_fill_price or trade.decision_close
            logger.warning(
                "trade %d closed without an attributable fill; estimating exit at %.2f",
                trade.id,
                fill_price,
            )

        return self._finalize(
            trade,
            exit_price=fill_price,
            exit_order_id=fill_id,
            closed_at=fill_at or now,
            reason=reason,
            closed_bars=closed_bars,
        )

    def _finalize(
        self,
        trade: TradeRecord,
        *,
        exit_price: float,
        exit_order_id: str | None,
        closed_at: datetime,
        reason: ExitReason,
        closed_bars: pd.DataFrame | None,
    ) -> TradeRecord:
        entry = trade.entry_fill_price or trade.decision_close
        gross = (exit_price - entry) * trade.units
        fees, estimated = self._fees_for(trade, entry=entry, exit_price=exit_price)
        pnl = gross - fees
        pnl_r = pnl / (trade.units * trade.sl_distance) if trade.sl_distance > 0 else 0.0

        if closed_bars is not None and not closed_bars.empty:
            exc = compute_mfe_mae(
                closed_bars,
                entry=entry,
                side=trade.side,
                sl_distance=trade.sl_distance,
                opened_at=trade.opened_at,
                closed_at=closed_at,
            )
        else:
            exc = compute_mfe_mae(
                pd.DataFrame(),
                entry=entry,
                side=trade.side,
                sl_distance=trade.sl_distance,
                opened_at=trade.opened_at,
                closed_at=closed_at,
            )

        bars_held = max(
            exc.bars_held, bars_between(trade.opened_at, closed_at, self._granularity)
        )

        trades_db.mark_closed(
            self._conn,
            trade.id,
            closed_at=closed_at,
            exit_order_id=exit_order_id,
            exit_price=exit_price,
            exit_reason=reason,
            pnl_usd=pnl,
            pnl_r=pnl_r,
            fees_usd=fees,
            fees_estimated=estimated,
            mfe=exc.mfe,
            mae=exc.mae,
            mfe_r=exc.mfe_r,
            mae_r=exc.mae_r,
            bars_held=bars_held,
        )

        entry_row = self._journal.write(
            kind=JournalKind.TRADE_CLOSED,
            payload=TradeClosedPayload(
                trade_id=trade.id,
                client_tag=trade.client_tag,
                side=trade.side,
                units=trade.units,
                opened_at=trade.opened_at,
                closed_at=closed_at,
                entry_fill_price=entry,
                exit_price=exit_price,
                exit_reason=reason,
                exit_order_id=exit_order_id,
                pnl_usd=pnl,
                pnl_r=pnl_r,
                fees_usd=fees,
                fees_estimated=estimated,
                mfe=exc.mfe,
                mae=exc.mae,
                mfe_r=exc.mfe_r,
                mae_r=exc.mae_r,
                bars_held=bars_held,
                stop_loss=trade.sl,
                take_profit=trade.tp,
                indicators=trade.indicators,
            ),
            strategy_version_id=trade.strategy_version_id,
            prev_state=JournalState.OPEN_PROTECTED,
            next_state=JournalState.TRADE_CLOSED,
            reason_codes=[reason.value],
        )
        if entry_row.id is not None:
            trades_db.set_journal_close_id(self._conn, trade.id, entry_row.id)

        self._through_stop_strikes.pop(trade.id, None)
        logger.info(
            "closed trade %d: %s at %.2f, pnl %.2f USD (%.2fR)",
            trade.id,
            reason.value,
            exit_price,
            pnl,
            pnl_r,
        )
        closed = trades_db.get(self._conn, trade.id)
        assert closed is not None
        return closed

    def _fees_for(
        self, trade: TradeRecord, *, entry: float, exit_price: float
    ) -> tuple[float, bool]:
        """Round-trip fee in USD, and whether it had to be estimated.

        Paper accounts may not book crypto FEE activities at all. Reporting a
        modelled fee flagged as estimated is honest; reporting zero is not.
        """
        notional = trade.units * (entry + exit_price)
        return notional * SIM.fee_bps / 10_000.0, True

    # ------------------------------------------------------------- helpers

    def _get_stop_order(self, trade: TradeRecord) -> Order | None:
        if not trade.sl_order_id:
            return None
        try:
            return self._broker.get_order(trade.sl_order_id)
        except BrokerError:
            return None

    def _replace_stop(
        self, trade: TradeRecord, *, qty: float, now: datetime
    ) -> Order | None:
        trades_db.set_status(self._conn, trade.id, TradeStatus.OPEN_UNPROTECTED)
        for attempt in range(1, 4):
            order = self._place_stop(
                trade_id=trade.id,
                client_tag=trade.client_tag,
                qty=qty,
                stop_price=trade.sl,
                attempt=attempt,
            )
            if order is not None:
                trades_db.set_status(
                    self._conn,
                    trade.id,
                    TradeStatus.OPEN_PROTECTED,
                    sl_order_id=order.id,
                )
                self._journal.write(
                    kind=JournalKind.RISK_EVENT,
                    payload=RiskEventPayload(
                        event=ReasonCode.STOP_ORDER_REPLACED,
                        detail=f"protective stop re-placed as order {order.id}",
                        trade_id=trade.id,
                        client_tag=trade.client_tag,
                        value=trade.sl,
                        action="position protected again",
                    ),
                    reason_codes=[ReasonCode.STOP_ORDER_REPLACED.value],
                )
                return order
        return None

    def _position_qty(self) -> float:
        try:
            positions = self._broker.list_positions()
        except BrokerError:
            return 0.0
        return sum(p.qty for p in positions if p.qty > 0)

    def _safe_bid(self) -> float | None:
        try:
            return self._broker.latest_quote(self._instrument).bid
        except BrokerError:
            return None

    def _config_for(self, trade: TradeRecord) -> StrategyConfig | None:
        from app.db import strategy_versions as sv

        version = sv.get(self._conn, trade.strategy_version_id)
        return version.config if version else None

    # ----------------------------------------------------------- reconcile

    def reconcile_on_boot(self, *, now: datetime | None = None) -> ReconcileReport:
        """Make the database and the broker agree before trading resumes.

        Four cases, and the dangerous one is the third: a position at the
        broker that this system has no record of. It cannot be managed, so it
        is closed rather than adopted.
        """
        moment = now or _now()
        report = ReconcileReport()

        db_open = trades_db.open_trades(self._conn)
        position_qty = self._position_qty()

        if db_open and position_qty > 0:
            trade = db_open[-1]
            stop = self._get_stop_order(trade)
            if stop is None or not stop.status.is_working:
                replaced = self._replace_stop(trade, qty=position_qty, now=moment)
                if replaced is not None:
                    report.stops_replaced.append(trade.id)
                else:
                    self._exit_now(trade, reason=ExitReason.UNPROTECTED, now=moment)
                    report.closed_from_broker.append(trade.id)
                    return report
            report.adopted.append(trade.id)
            # Any older open rows cannot correspond to a real position.
            for stale in db_open[:-1]:
                self._close_from_broker_history(stale, closed_bars=None, now=moment)
                report.closed_from_broker.append(stale.id)

        elif db_open and position_qty <= 0:
            for trade in db_open:
                self._close_from_broker_history(trade, closed_bars=None, now=moment)
                report.closed_from_broker.append(trade.id)

        elif not db_open and position_qty > 0:
            logger.error(
                "orphan position of %.4f units with no database record; flattening",
                position_qty,
            )
            self._journal.write(
                kind=JournalKind.RISK_EVENT,
                payload=RiskEventPayload(
                    event=ReasonCode.ORPHAN_POSITION,
                    detail="broker holds a position this system has no record of",
                    value=position_qty,
                    action="flattened at market",
                ),
                reason_codes=[ReasonCode.ORPHAN_POSITION.value],
            )
            try:
                self._broker.close_position(self._instrument)
                report.orphans_flattened = 1
            except (BrokerError, PermissionError) as exc:
                logger.error("could not flatten orphan position: %s", exc)

        # Cancel anything still resting that no open trade owns.
        live_stop_ids = {
            t.sl_order_id for t in trades_db.open_trades(self._conn) if t.sl_order_id
        }
        try:
            for o in self._broker.list_orders(status="open", limit=100):
                if o.id in live_stop_ids:
                    continue
                self._broker.cancel_order(o.id)
                report.stray_orders_cancelled += 1
        except (BrokerError, PermissionError) as exc:
            logger.warning("could not sweep stray orders: %s", exc)

        if not report.clean:
            self._journal.write(
                kind=JournalKind.SYSTEM_EVENT,
                payload=SystemEventPayload(
                    event="boot_reconcile",
                    detail="broker and database reconciled at startup",
                    data={
                        "adopted": report.adopted,
                        "closed_from_broker": report.closed_from_broker,
                        "orphans_flattened": report.orphans_flattened,
                        "stray_orders_cancelled": report.stray_orders_cancelled,
                        "stops_replaced": report.stops_replaced,
                    },
                ),
            )
        return report

    def flatten_all(self, *, reason: ExitReason = ExitReason.MANUAL) -> list[TradeRecord]:
        """Close every open position now. Used by `cli flatten` and shutdown."""
        out: list[TradeRecord] = []
        for trade in trades_db.open_trades(self._conn):
            closed = self._exit_now(trade, reason=reason, now=_now())
            if closed is not None:
                out.append(closed)
        return out
