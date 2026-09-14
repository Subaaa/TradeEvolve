"""Thin wrapper over the Alpaca paper-trading REST API.

The MCP server is the only component that knows the Alpaca API key.
This module also owns the one broker-specific translation the rest of
the app should never need to think about: mapping the app's canonical
instrument name (`XAU_USD`) to whatever symbol the connected account
actually trades (`cfg.symbol`, e.g. "PAXG/USD" -- Pax Gold, 1 token = 1
troy oz of gold, used as the XAU/USD proxy since Alpaca has no forex/CFD
gold market).

Candle and price responses are shaped to match what the original OANDA
client returned (`{"candles": [{"time", "mid": {...}, "volume",
"complete"}]}`, `{"instrument", "bid", "ask", "time"}`) so
`app/market/data_source.py` does not need to know which broker is on
the other end.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from .config import AlpacaMcpConfig

_GRANULARITY_TO_TIMEFRAME = {
    "M1": "1Min",
    "M5": "5Min",
    "M15": "15Min",
    "M30": "30Min",
    "H1": "1Hour",
    "H4": "4Hour",
    "D": "1Day",
    "W": "1Week",
}


class AlpacaClient:
    def __init__(self, cfg: AlpacaMcpConfig) -> None:
        if not cfg.api_key_id or not cfg.api_secret_key:
            raise RuntimeError("Alpaca credentials not set in MCP subprocess env")
        if "paper-api" not in cfg.base_url:
            raise PermissionError(
                "refusing to connect: ALPACA_BASE_URL is not a paper-trading URL "
                "(LIVE_TRADING_ENABLED is false)"
            )
        self._cfg = cfg
        headers = {
            "APCA-API-KEY-ID": cfg.api_key_id,
            "APCA-API-SECRET-KEY": cfg.api_secret_key,
        }
        self._trading = httpx.Client(base_url=cfg.base_url, headers=headers, timeout=15.0)
        self._data = httpx.Client(base_url=cfg.data_url, headers=headers, timeout=15.0)

    def close(self) -> None:
        self._trading.close()
        self._data.close()

    def _resolve_symbol(self, instrument: str) -> str:
        """Map the app's canonical instrument name to the Alpaca symbol.

        The MVP trades a single configured instrument; anything else is
        passed through unchanged (useful for `get_instruments`/tests).
        """
        return self._cfg.symbol if instrument in ("XAU_USD", self._cfg.symbol) else instrument

    def list_instruments(self) -> list[dict[str, Any]]:
        return [{
            "name": "XAU_USD",
            "type": "CRYPTO",
            "displayName": f"Pax Gold ({self._cfg.symbol})",
            "pipLocation": -2,
        }]

    def fetch_candles(
        self,
        instrument: str,
        granularity: str,
        *,
        from_: datetime | None = None,
        to: datetime | None = None,
        count: int | None = None,
    ) -> list[dict[str, Any]]:
        symbol = self._resolve_symbol(instrument)
        params: dict[str, Any] = {
            "symbols": symbol,
            "timeframe": _GRANULARITY_TO_TIMEFRAME[granularity],
            "limit": count or 500,
        }
        if from_ is not None:
            params["start"] = from_.isoformat()
        if to is not None:
            params["end"] = to.isoformat()
        r = self._data.get("/v1beta3/crypto/us/bars", params=params)
        r.raise_for_status()
        bars = r.json().get("bars", {}).get(symbol, [])
        out = []
        for b in bars:
            out.append({
                "time": b["t"],
                "mid": {
                    "o": float(b["o"]),
                    "h": float(b["h"]),
                    "l": float(b["l"]),
                    "c": float(b["c"]),
                },
                "volume": int(b.get("v", 0)),
                "complete": True,
            })
        return out

    def latest_price(self, instrument: str) -> dict[str, Any]:
        symbol = self._resolve_symbol(instrument)
        r = self._data.get("/v1beta3/crypto/us/latest/orderbooks", params={"symbols": symbol})
        r.raise_for_status()
        book = r.json().get("orderbooks", {}).get(symbol)
        if not book or not book.get("b") or not book.get("a"):
            raise RuntimeError(f"no orderbook returned for {symbol}")
        return {
            "instrument": instrument,
            "bid": float(book["b"][0]["p"]),
            "ask": float(book["a"][0]["p"]),
            "time": book["t"],
        }

    def account_summary(self) -> dict[str, Any]:
        r = self._trading.get("/v2/account")
        r.raise_for_status()
        a = r.json()
        return {
            "cash": float(a["cash"]),
            "equity": float(a["equity"]),
            "portfolio_value": float(a["portfolio_value"]),
            "buying_power": float(a["buying_power"]),
            "currency": a["currency"],
            "status": a["status"],
            "crypto_status": a.get("crypto_status"),
        }

    def open_positions(self) -> list[dict[str, Any]]:
        r = self._trading.get("/v2/positions")
        r.raise_for_status()
        out = []
        for p in r.json():
            out.append({
                "symbol": p["symbol"],
                "side": p["side"],
                "qty": float(p["qty"]),
                "avg_entry_price": float(p["avg_entry_price"]),
                "market_value": float(p["market_value"]),
                "unrealized_pl": float(p["unrealized_pl"]),
            })
        return out

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        if not self._cfg.write_enabled:
            raise PermissionError("place_order is disabled in this MCP subprocess")
        symbol = self._resolve_symbol(order["instrument"])
        body: dict[str, Any] = {
            "symbol": symbol,
            "qty": str(order["qty"]),
            "side": order["side"],
            "type": "market",
            "time_in_force": "gtc",
        }
        if order.get("client_order_id"):
            body["client_order_id"] = order["client_order_id"]
        r = self._trading.post("/v2/orders", json=body)
        r.raise_for_status()
        o = r.json()
        return {
            "order_id": o["id"],
            "client_order_id": o.get("client_order_id", ""),
            "status": o["status"],
            "filled_qty": float(o.get("filled_qty") or 0),
            "filled_avg_price": float(o["filled_avg_price"]) if o.get("filled_avg_price") else None,
        }

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not self._cfg.write_enabled:
            raise PermissionError("cancel_order is disabled in this MCP subprocess")
        r = self._trading.delete(f"/v2/orders/{order_id}")
        if r.status_code not in (200, 204):
            r.raise_for_status()
        return {"order_id": order_id, "status": "cancel_requested"}
