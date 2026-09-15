"""Alpaca paper-trading REST client, in process.

This module owns the one broker-specific translation the rest of the app
should never think about: mapping the canonical instrument name (`XAU_USD`)
to the symbol the connected account actually trades (`PAXG/USD` — Pax Gold,
1 token = 1 troy oz, used as the XAU/USD proxy because Alpaca has no
forex/CFD gold market).

Two guards, both unconditional:

* the constructor refuses any base URL that is not a paper-trading URL;
* every write refuses unless `write_enabled` was passed explicitly.

Crypto-specific constraints encoded here, from Alpaca's own docs:

* order types are limited to `market`, `limit` and `stop_limit` — there is
  no plain `stop`, which is why protective stops are `stop_limit` with a
  band below the trigger;
* `time_in_force` is limited to `gtc` and `ioc`;
* crypto cannot be sold short, so the executor is long-only.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from pinned.simulation_constants import LIVE_TRADING_ENABLED

from .types import (
    Account,
    Bar,
    Broker,
    BrokerError,
    BrokerOrderType,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Position,
    Quote,
)

logger = logging.getLogger(__name__)

_GRANULARITY_TO_TIMEFRAME: dict[str, str] = {
    "M1": "1Min",
    "M5": "5Min",
    "M15": "15Min",
    "M30": "30Min",
    "H1": "1Hour",
    "H4": "4Hour",
    "D": "1Day",
    "W": "1Week",
}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    # Alpaca sometimes returns more than 6 fractional digits, which
    # `fromisoformat` rejects on 3.11.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                digits += ch
            else:
                rest = tail[i:]
                break
        text = f"{head}.{digits[:6]}{rest}"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _require_ts(value: str | None, *, field: str) -> datetime:
    parsed = _parse_ts(value)
    if parsed is None:
        raise BrokerError(f"missing timestamp field '{field}' in broker response")
    return parsed


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _opt_f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


class AlpacaClient(Broker):
    """Paper-only Alpaca client."""

    def __init__(
        self,
        *,
        api_key_id: str,
        api_secret_key: str,
        base_url: str,
        data_url: str,
        symbol: str,
        write_enabled: bool = False,
        timeout: float = 15.0,
    ) -> None:
        if not api_key_id or not api_secret_key:
            raise BrokerError("Alpaca credentials are not set")
        if "paper-api" not in base_url:
            raise PermissionError(
                "refusing to connect: ALPACA_BASE_URL is not a paper-trading URL "
                f"(LIVE_TRADING_ENABLED is {LIVE_TRADING_ENABLED})"
            )
        if LIVE_TRADING_ENABLED:  # pragma: no cover - the constant is pinned False
            raise PermissionError("LIVE_TRADING_ENABLED must be False in this build")

        self._symbol = symbol
        self._write_enabled = write_enabled
        headers = {
            "APCA-API-KEY-ID": api_key_id,
            "APCA-API-SECRET-KEY": api_secret_key,
        }
        self._trading = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)
        self._data = httpx.Client(base_url=data_url, headers=headers, timeout=timeout)

    # ---- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self._trading.close()
        self._data.close()

    def __enter__(self) -> AlpacaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def write_enabled(self) -> bool:
        return self._write_enabled

    # ---- helpers ---------------------------------------------------------

    def resolve_symbol(self, instrument: str) -> str:
        """Map the canonical instrument name to the Alpaca symbol."""
        return self._symbol if instrument in ("XAU_USD", self._symbol) else instrument

    @staticmethod
    def _position_path_symbol(symbol: str) -> str:
        """Alpaca's position endpoints take the symbol without the slash."""
        return symbol.replace("/", "")

    def _check_write(self, action: str) -> None:
        if not self._write_enabled:
            raise PermissionError(f"{action} is disabled (ALPACA_WRITE_ENABLED is false)")

    @staticmethod
    def _raise_for_status(r: httpx.Response, what: str) -> None:
        if r.is_success:
            return
        raise BrokerError(
            f"{what} failed: HTTP {r.status_code} {r.text[:400]}",
            status_code=r.status_code,
        )

    # ---- reads -----------------------------------------------------------

    def get_account(self) -> Account:
        r = self._trading.get("/v2/account")
        self._raise_for_status(r, "get_account")
        a = r.json()
        return Account(
            cash=_f(a.get("cash")),
            equity=_f(a.get("equity")),
            portfolio_value=_f(a.get("portfolio_value")),
            buying_power=_f(a.get("buying_power")),
            currency=a.get("currency", "USD"),
            status=a.get("status", "UNKNOWN"),
            crypto_status=a.get("crypto_status"),
        )

    def list_positions(self) -> list[Position]:
        r = self._trading.get("/v2/positions")
        self._raise_for_status(r, "list_positions")
        return [
            Position(
                symbol=p["symbol"],
                side=p["side"],
                qty=_f(p.get("qty")),
                avg_entry_price=_f(p.get("avg_entry_price")),
                market_value=_f(p.get("market_value")),
                unrealized_pl=_f(p.get("unrealized_pl")),
            )
            for p in r.json()
        ]

    def latest_quote(self, instrument: str) -> Quote:
        symbol = self.resolve_symbol(instrument)
        r = self._data.get(
            "/v1beta3/crypto/us/latest/quotes", params={"symbols": symbol}
        )
        self._raise_for_status(r, "latest_quote")
        q = r.json().get("quotes", {}).get(symbol)
        if not q:
            raise BrokerError(f"no quote returned for {symbol}")
        return Quote(
            symbol=symbol,
            bid=_f(q.get("bp")),
            ask=_f(q.get("ap")),
            ts=_require_ts(q.get("t"), field="t"),
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
        """Fetch bars, following `next_page_token` until `limit` is reached.

        Volume is kept as a float: PAXG bar volumes are fractional, and the
        previous `int()` cast silently zeroed every one of them.
        """
        symbol = self.resolve_symbol(instrument)
        timeframe = _GRANULARITY_TO_TIMEFRAME.get(granularity)
        if timeframe is None:
            raise BrokerError(f"unsupported granularity {granularity!r}")

        out: list[Bar] = []
        page_token: str | None = None
        while len(out) < limit:
            params: dict[str, Any] = {
                "symbols": symbol,
                "timeframe": timeframe,
                "limit": min(10_000, limit - len(out)),
                "sort": "asc",
            }
            if start is not None:
                params["start"] = start.astimezone(UTC).isoformat()
            if end is not None:
                params["end"] = end.astimezone(UTC).isoformat()
            if page_token:
                params["page_token"] = page_token

            r = self._data.get("/v1beta3/crypto/us/bars", params=params)
            self._raise_for_status(r, "fetch_bars")
            body = r.json()
            bars = body.get("bars", {}).get(symbol) or []
            for b in bars:
                out.append(
                    Bar(
                        ts=_require_ts(b["t"], field="t"),
                        o=_f(b["o"]),
                        h=_f(b["h"]),
                        l=_f(b["l"]),
                        c=_f(b["c"]),
                        v=_f(b.get("v")),
                    )
                )
            page_token = body.get("next_page_token")
            if not page_token or not bars:
                break
        return out[:limit]

    # ---- orders ----------------------------------------------------------

    def _parse_order(self, o: dict[str, Any]) -> Order:
        return Order(
            id=str(o["id"]),
            client_order_id=o.get("client_order_id", ""),
            symbol=o.get("symbol", ""),
            side=OrderSide(o["side"]),
            type=BrokerOrderType(o.get("type", "market")),
            status=OrderStatus(o.get("status", "new")),
            qty=_f(o.get("qty")),
            filled_qty=_f(o.get("filled_qty")),
            filled_avg_price=_opt_f(o.get("filled_avg_price")),
            submitted_at=_parse_ts(o.get("submitted_at")),
            filled_at=_parse_ts(o.get("filled_at")),
            stop_price=_opt_f(o.get("stop_price")),
            limit_price=_opt_f(o.get("limit_price")),
        )

    def submit_order(self, req: OrderRequest) -> Order:
        self._check_write("submit_order")
        if req.side == OrderSide.SELL and req.type == BrokerOrderType.MARKET:
            pass  # closing a long is a sell; that is not a short
        body: dict[str, Any] = {
            "symbol": self.resolve_symbol(req.symbol),
            "qty": f"{req.qty:.9f}".rstrip("0").rstrip("."),
            "side": req.side.value,
            "type": req.type.value,
            "time_in_force": req.time_in_force,
            "client_order_id": req.client_order_id,
        }
        if req.limit_price is not None:
            body["limit_price"] = f"{req.limit_price:.2f}"
        if req.stop_price is not None:
            body["stop_price"] = f"{req.stop_price:.2f}"
        r = self._trading.post("/v2/orders", json=body)
        self._raise_for_status(r, "submit_order")
        return self._parse_order(r.json())

    def submit_raw_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """Escape hatch used only by `broker-probe` to test bracket support."""
        self._check_write("submit_raw_order")
        r = self._trading.post("/v2/orders", json=body)
        self._raise_for_status(r, "submit_raw_order")
        result: dict[str, Any] = r.json()
        return result

    def get_order(self, order_id: str) -> Order:
        r = self._trading.get(f"/v2/orders/{order_id}")
        self._raise_for_status(r, "get_order")
        return self._parse_order(r.json())

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        r = self._trading.get(
            "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id}
        )
        if r.status_code == 404:
            return None
        self._raise_for_status(r, "get_order_by_client_id")
        return self._parse_order(r.json())

    def list_orders(
        self,
        *,
        status: str = "open",
        after: datetime | None = None,
        limit: int = 100,
    ) -> list[Order]:
        params: dict[str, Any] = {"status": status, "limit": limit, "direction": "desc"}
        if after is not None:
            params["after"] = after.astimezone(UTC).isoformat()
        r = self._trading.get("/v2/orders", params=params)
        self._raise_for_status(r, "list_orders")
        return [self._parse_order(o) for o in r.json()]

    def cancel_order(self, order_id: str) -> None:
        self._check_write("cancel_order")
        r = self._trading.delete(f"/v2/orders/{order_id}")
        # 404/422 mean it is already gone or already terminal; both are fine.
        if r.status_code in (200, 204, 404, 422):
            return
        self._raise_for_status(r, "cancel_order")

    def close_position(self, instrument: str) -> Order | None:
        self._check_write("close_position")
        symbol = self._position_path_symbol(self.resolve_symbol(instrument))
        r = self._trading.delete(f"/v2/positions/{symbol}")
        if r.status_code == 404:
            return None
        self._raise_for_status(r, "close_position")
        return self._parse_order(r.json())

    # ---- account activity ------------------------------------------------

    def list_fee_activities(self, *, after: datetime) -> list[dict[str, Any]]:
        """Return FEE activities since `after`, or [] if the account books none.

        Paper accounts may not record crypto fees at all; callers treat an
        empty result as "estimate the fee" rather than "the fee was zero".
        """
        r = self._trading.get(
            "/v2/account/activities",
            params={
                "activity_types": "FEE",
                "after": after.astimezone(UTC).isoformat(),
            },
        )
        if r.status_code == 404:
            return []
        self._raise_for_status(r, "list_fee_activities")
        body = r.json()
        return list(body) if isinstance(body, list) else []


def build_client(settings: Any, *, write_enabled: bool | None = None) -> AlpacaClient:
    """Construct an `AlpacaClient` from `Settings`."""
    return AlpacaClient(
        api_key_id=settings.alpaca_api_key_id,
        api_secret_key=settings.alpaca_api_secret_key,
        base_url=settings.alpaca_base_url,
        data_url=settings.alpaca_data_url,
        symbol=settings.alpaca_symbol,
        write_enabled=(
            settings.alpaca_write_enabled if write_enabled is None else write_enabled
        ),
    )
