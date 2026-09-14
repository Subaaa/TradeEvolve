"""Postgres-backed running account state (the `system_state` table).

Two rows: `account_running_state` (equity_high / day_pnl / week_pnl /
consecutive_losses / last_trade_close_ts / last_n_trade_outcomes --
everything `AccountState` needs that the broker doesn't report) and
`kill_switch` (its own row, matching docs/architecture.md: "a row in
`system_state`; the live loop checks it every tick").

`equity` and `open_positions` are *not* stored here -- they're read
fresh from the broker every tick (single source of truth, no local
drift); see `app/scheduler/jobs/tick_live_loop.py`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.engine import Engine

_RUNNING_STATE_KEY = "account_running_state"
_KILL_SWITCH_KEY = "kill_switch"


@dataclass
class RunningState:
    equity_high: float = 0.0
    day_pnl: float = 0.0
    week_pnl: float = 0.0
    consecutive_losses: int = 0
    last_trade_close_ts: datetime | None = None
    last_n_trade_outcomes: list[Literal["win", "loss", "breakeven"]] = field(default_factory=list)


def _today_utc() -> str:
    return datetime.now(tz=UTC).date().isoformat()


def _iso_week_utc() -> str:
    y, w, _ = datetime.now(tz=UTC).isocalendar()
    return f"{y}-W{w:02d}"


def load_running_state(engine: Engine) -> RunningState:
    with engine.connect() as conn:
        row = conn.execute(
            text("select value from system_state where key = :k"), {"k": _RUNNING_STATE_KEY}
        ).scalar()
    if row is None:
        return RunningState()
    v = json.loads(row) if isinstance(row, str) else row
    day_pnl = float(v.get("day_pnl", 0.0)) if v.get("day_anchor") == _today_utc() else 0.0
    week_pnl = float(v.get("week_pnl", 0.0)) if v.get("week_anchor") == _iso_week_utc() else 0.0
    last_close = v.get("last_trade_close_ts")
    return RunningState(
        equity_high=float(v.get("equity_high", 0.0)),
        day_pnl=day_pnl,
        week_pnl=week_pnl,
        consecutive_losses=int(v.get("consecutive_losses", 0)),
        last_trade_close_ts=datetime.fromisoformat(last_close) if last_close else None,
        last_n_trade_outcomes=list(v.get("last_n_trade_outcomes", [])),
    )


def save_running_state(engine: Engine, state: RunningState) -> None:
    payload = {
        "equity_high": state.equity_high,
        "day_pnl": state.day_pnl,
        "week_pnl": state.week_pnl,
        "consecutive_losses": state.consecutive_losses,
        "last_trade_close_ts": state.last_trade_close_ts.isoformat()
        if state.last_trade_close_ts
        else None,
        "last_n_trade_outcomes": state.last_n_trade_outcomes,
        "day_anchor": _today_utc(),
        "week_anchor": _iso_week_utc(),
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                insert into system_state (key, value, updated_at)
                values (:key, cast(:value as jsonb), now())
                on conflict (key) do update set value = excluded.value, updated_at = now()
                """
            ),
            {"key": _RUNNING_STATE_KEY, "value": json.dumps(payload, default=str)},
        )


def load_kill_switch(engine: Engine) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("select value from system_state where key = :k"), {"k": _KILL_SWITCH_KEY}
        ).scalar()
    if row is None:
        return False
    v = json.loads(row) if isinstance(row, str) else row
    return bool(v.get("engaged", False))


def save_kill_switch(engine: Engine, engaged: bool, *, reason: str = "") -> None:
    payload = {"engaged": engaged, "reason": reason}
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                insert into system_state (key, value, updated_at)
                values (:key, cast(:value as jsonb), now())
                on conflict (key) do update set value = excluded.value, updated_at = now()
                """
            ),
            {"key": _KILL_SWITCH_KEY, "value": json.dumps(payload)},
        )
