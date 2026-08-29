"""Alerts: drain the alert queue and post to ALERT_WEBHOOK_URL.

In MVP this is a thin wrapper. The webhook URL is optional; if unset,
alerts are logged only.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx


logger = logging.getLogger(__name__)


class AlertStore(Protocol):
    def pop_pending(self) -> list[dict[str, Any]]: ...


class Alerter:
    def __init__(self, store: AlertStore, *, webhook_url: str = "", timeout_sec: float = 5.0) -> None:
        self._store = store
        self._url = webhook_url
        self._timeout = timeout_sec

    def drain(self) -> int:
        items = self._store.pop_pending()
        n = 0
        for item in items:
            self._emit(item)
            n += 1
        return n

    def _emit(self, item: dict[str, Any]) -> None:
        if not self._url:
            logger.warning("alert (no webhook): %s", item)
            return
        try:
            resp = httpx.post(self._url, json=item, timeout=self._timeout)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("alert webhook failed: %s", e)
