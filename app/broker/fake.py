"""In-memory broker for tests.

Deterministic: no clock of its own beyond what the caller passes, no
network. `advance(bar)` walks the simulated market forward one bar and
triggers any resting stop_limit order whose stop price the bar traded
through, which is what makes the position-manager tests meaningful.
"""
from __future__ import annotations

import itertools
from datetime import UTC, datetime

from .types import (
    Account,
    Bar,
    BrokerError,
    BrokerOrderType,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Position,
    Quote,
)


class FakeBroker:
    """A minimal long-only spot broker."""

    def __init__(
        self,
        *,
        symbol: str = "PAXG/USD",
        equity: float = 10_000.0,
        price: float = 4_000.0,
        write_enabled: bool = True,
    ) -> None:
        self.symbol = symbol
        self._equity = equity
        self._cash = equity
        self._price = price
        self._write_enabled = write_enabled
        self._qty = 0.0
        self._avg_entry = 0.0
        self._orders: dict[str, Order] = {}
        self._ids = itertools.count(1)
        self._bars: list[Bar] = []
        self.now: datetime = datetime(2026, 1, 1, tzinfo=UTC)
        # Set to make submit_order raise, to exercise the flatten-on-failure path.
        self.fail_next_stop_order = False
        self.reject_next_entry = False

    # ---- test helpers ----------------------------------------------------

    def set_price(self, price: float) -> None:
        self._price = price

    def advance(self, bar: Bar) -> None:
        """Move the market one bar forward, filling any triggered stop."""
        self._bars.append(bar)
        self._price = bar.c
        self.now = bar.ts
        for oid, o in list(self._orders.items()):
            if not o.status.is_working or o.type is not BrokerOrderType.STOP_LIMIT:
                continue
            stop = o.stop_price or 0.0
            if o.side is OrderSide.SELL and bar.l <= stop:
                fill = min(stop, bar.h)
                self._orders[oid] = o.model_copy(
                    update={
                        "status": OrderStatus.FILLED,
                        "filled_qty": o.qty,
                        "filled_avg_price": fill,
                        "filled_at": bar.ts,
                    }
                )
                self._settle_sell(o.qty, fill)

    def _settle_sell(self, qty: float, price: float) -> None:
        realized = (price - self._avg_entry) * qty
        self._qty = max(0.0, self._qty - qty)
        self._cash += realized
        self._equity += realized
        if self._qty <= 0.0:
            self._avg_entry = 0.0

    # ---- Broker protocol -------------------------------------------------

    def get_account(self) -> Account:
        unrealized = (self._price - self._avg_entry) * self._qty if self._qty else 0.0
        return Account(
            cash=self._cash,
            equity=self._equity + unrealized,
            portfolio_value=self._equity + unrealized,
            buying_power=self._cash,
            currency="USD",
            status="ACTIVE",
            crypto_status="ACTIVE",
        )

    def list_positions(self) -> list[Position]:
        if self._qty <= 0.0:
            return []
        return [
            Position(
                symbol=self.symbol,
                side="long",
                qty=self._qty,
                avg_entry_price=self._avg_entry,
                market_value=self._qty * self._price,
                unrealized_pl=(self._price - self._avg_entry) * self._qty,
            )
        ]

    def latest_quote(self, instrument: str) -> Quote:
        return Quote(
            symbol=self.symbol,
            bid=self._price * 0.9998,
            ask=self._price * 1.0002,
            ts=self.now,
        )

    def fetch_bars(
        self,
        instrument: str,
        granularity: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[Bar]:
        bars = self._bars
        if start is not None:
            bars = [b for b in bars if b.ts >= start]
        if end is not None:
            bars = [b for b in bars if b.ts <= end]
        return bars[-limit:]

    def submit_order(self, req: OrderRequest) -> Order:
        if not self._write_enabled:
            raise PermissionError("submit_order is disabled")
        is_stop = req.side is OrderSide.SELL and req.type is BrokerOrderType.STOP_LIMIT
        if is_stop and self.fail_next_stop_order:
            self.fail_next_stop_order = False
            raise BrokerError("simulated stop placement failure", status_code=422)
        if req.side is OrderSide.BUY and self.reject_next_entry:
            self.reject_next_entry = False
            order = Order(
                id=str(next(self._ids)),
                client_order_id=req.client_order_id,
                symbol=self.symbol,
                side=req.side,
                type=req.type,
                status=OrderStatus.REJECTED,
                qty=req.qty,
                filled_qty=0.0,
                filled_avg_price=None,
                submitted_at=self.now,
            )
            self._orders[order.id] = order
            return order

        oid = str(next(self._ids))
        if req.type is BrokerOrderType.MARKET:
            fill = self._price
            if req.side is OrderSide.BUY:
                total = self._avg_entry * self._qty + fill * req.qty
                self._qty += req.qty
                self._avg_entry = total / self._qty
                self._cash -= fill * req.qty
            else:
                self._settle_sell(req.qty, fill)
            order = Order(
                id=oid,
                client_order_id=req.client_order_id,
                symbol=self.symbol,
                side=req.side,
                type=req.type,
                status=OrderStatus.FILLED,
                qty=req.qty,
                filled_qty=req.qty,
                filled_avg_price=fill,
                submitted_at=self.now,
                filled_at=self.now,
            )
        else:
            order = Order(
                id=oid,
                client_order_id=req.client_order_id,
                symbol=self.symbol,
                side=req.side,
                type=req.type,
                status=OrderStatus.NEW,
                qty=req.qty,
                filled_qty=0.0,
                filled_avg_price=None,
                submitted_at=self.now,
                stop_price=req.stop_price,
                limit_price=req.limit_price,
            )
        self._orders[oid] = order
        return order

    def get_order(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise BrokerError(f"unknown order {order_id}", status_code=404) from None

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        for o in self._orders.values():
            if o.client_order_id == client_order_id:
                return o
        return None

    def list_orders(
        self,
        *,
        status: str = "open",
        after: datetime | None = None,
        limit: int = 100,
    ) -> list[Order]:
        orders = list(self._orders.values())
        if status == "open":
            orders = [o for o in orders if o.status.is_working]
        elif status == "closed":
            orders = [o for o in orders if o.status.is_terminal]
        if after is not None:
            orders = [
                o for o in orders if o.submitted_at is not None and o.submitted_at >= after
            ]
        return orders[:limit]

    def cancel_order(self, order_id: str) -> None:
        o = self._orders.get(order_id)
        if o is not None and o.status.is_working:
            self._orders[order_id] = o.model_copy(update={"status": OrderStatus.CANCELED})

    def close_position(self, instrument: str) -> Order | None:
        if self._qty <= 0.0:
            return None
        qty = self._qty
        return self.submit_order(
            OrderRequest(
                symbol=self.symbol,
                qty=qty,
                side=OrderSide.SELL,
                type=BrokerOrderType.MARKET,
                time_in_force="ioc",
                client_order_id=f"fake-close-{next(self._ids)}",
            )
        )

    def list_fee_activities(self, *, after: datetime) -> list[dict[str, object]]:
        return []

    def adopt_position(self, qty: float, avg_entry: float) -> None:
        """Seed an existing position, to test boot reconciliation."""
        self._qty = qty
        self._avg_entry = avg_entry
