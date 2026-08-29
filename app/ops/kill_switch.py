"""Kill switch: a system_state row that, when true, halts the live loop."""
from __future__ import annotations

import logging
from typing import Protocol


logger = logging.getLogger(__name__)


class SystemStateStore(Protocol):
    def get(self, key: str) -> object | None: ...
    def set(self, key: str, value: object) -> None: ...


KILL_SWITCH_KEY = "kill_switch"


def is_engaged(store: SystemStateStore) -> bool:
    val = store.get(KILL_SWITCH_KEY)
    return bool(val)


def engage(store: SystemStateStore, *, reason: str) -> None:
    store.set(KILL_SWITCH_KEY, {"engaged": True, "reason": reason})
    logger.error("kill switch engaged: %s", reason)


def clear(store: SystemStateStore) -> None:
    store.set(KILL_SWITCH_KEY, {"engaged": False})
    logger.warning("kill switch cleared")
