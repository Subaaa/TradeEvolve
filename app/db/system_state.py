"""The `system_state` key/value table.

Holds everything the process needs to survive a restart: the kill switch,
the day and week equity anchors, the equity high-water mark, and the last
bar the strategy actually acted on.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from .sqlite import from_iso, to_iso, utcnow, utcnow_iso

KILL_SWITCH = "kill_switch"
DAY_ANCHOR = "day_anchor"
WEEK_ANCHOR = "week_anchor"
EQUITY_HIGH = "equity_high"
LAST_PROCESSED_BAR_TS = "last_processed_bar_ts"
LAST_TICK = "last_tick"
BROKER_CAPABILITIES = "broker_capabilities"


def get(conn: sqlite3.Connection, key: str) -> Any | None:
    row = conn.execute(
        "SELECT value FROM system_state WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["value"])


def set_(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO system_state(key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at",
        (key, json.dumps(value, default=str), utcnow_iso()),
    )


# ---------------------------------------------------------------- kill switch


class KillSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engaged: bool = False
    reason: str = ""
    requires_manual_clear: bool = False
    engaged_at: datetime | None = None

    @property
    def auto_clearable(self) -> bool:
        return self.engaged and not self.requires_manual_clear


def load_kill_switch(conn: sqlite3.Connection) -> KillSwitch:
    raw = get(conn, KILL_SWITCH)
    if not raw:
        return KillSwitch()
    return KillSwitch.model_validate(raw)


def save_kill_switch(conn: sqlite3.Connection, ks: KillSwitch) -> None:
    set_(conn, KILL_SWITCH, ks.model_dump(mode="json"))


# -------------------------------------------------------------------- anchors


class DayAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: date
    start_equity: float


class WeekAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iso_week: str
    start_equity: float


def load_day_anchor(conn: sqlite3.Connection) -> DayAnchor | None:
    raw = get(conn, DAY_ANCHOR)
    return DayAnchor.model_validate(raw) if raw else None


def save_day_anchor(conn: sqlite3.Connection, anchor: DayAnchor) -> None:
    set_(conn, DAY_ANCHOR, anchor.model_dump(mode="json"))


def load_week_anchor(conn: sqlite3.Connection) -> WeekAnchor | None:
    raw = get(conn, WEEK_ANCHOR)
    return WeekAnchor.model_validate(raw) if raw else None


def save_week_anchor(conn: sqlite3.Connection, anchor: WeekAnchor) -> None:
    set_(conn, WEEK_ANCHOR, anchor.model_dump(mode="json"))


def iso_week_of(moment: datetime) -> str:
    y, w, _ = moment.astimezone(UTC).isocalendar()
    return f"{y}-W{w:02d}"


def ensure_anchors(
    conn: sqlite3.Connection, *, equity: float, now: datetime | None = None
) -> tuple[DayAnchor, WeekAnchor]:
    """Return today's anchors, creating or rolling them over as needed.

    The daily-loss limit is measured against `DayAnchor.start_equity`, so this
    is the function that decides when "today" starts. It is UTC-midnight, the
    same boundary the kill switch auto-clears on.
    """
    now = now or utcnow()
    today = now.astimezone(UTC).date()
    week = iso_week_of(now)

    day = load_day_anchor(conn)
    if day is None or day.day != today:
        day = DayAnchor(day=today, start_equity=equity)
        save_day_anchor(conn, day)

    wk = load_week_anchor(conn)
    if wk is None or wk.iso_week != week:
        wk = WeekAnchor(iso_week=week, start_equity=equity)
        save_week_anchor(conn, wk)

    return day, wk


# ------------------------------------------------------------ equity / ticks


def load_equity_high(conn: sqlite3.Connection, *, default: float) -> float:
    raw = get(conn, EQUITY_HIGH)
    return float(raw) if raw is not None else default


def save_equity_high(conn: sqlite3.Connection, value: float) -> None:
    set_(conn, EQUITY_HIGH, value)


def bump_equity_high(conn: sqlite3.Connection, equity: float) -> float:
    high = max(load_equity_high(conn, default=equity), equity)
    save_equity_high(conn, high)
    return high


def load_last_processed_bar_ts(conn: sqlite3.Connection) -> datetime | None:
    raw = get(conn, LAST_PROCESSED_BAR_TS)
    return from_iso(raw) if isinstance(raw, str) else None


def save_last_processed_bar_ts(conn: sqlite3.Connection, ts: datetime) -> None:
    set_(conn, LAST_PROCESSED_BAR_TS, to_iso(ts))


def touch_last_tick(conn: sqlite3.Connection, *, now: datetime | None = None) -> None:
    set_(conn, LAST_TICK, to_iso(now or utcnow()))


def load_last_tick(conn: sqlite3.Connection) -> datetime | None:
    raw = get(conn, LAST_TICK)
    return from_iso(raw) if isinstance(raw, str) else None
