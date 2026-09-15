"""The `trades` table: one row per position, opened and closed.

This is the evidence base. Every discipline counter the RiskEngine reads is
derived from these rows, and every fact the weekly LLM review reasons about
comes from here. A trade that is not written here did not happen as far as
the rest of the system is concerned.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.risk.types import ExitReason, Side

from .sqlite import from_iso, require_iso, to_iso


class TradeStatus(StrEnum):
    PENDING_ENTRY = "PENDING_ENTRY"
    OPEN_UNPROTECTED = "OPEN_UNPROTECTED"
    OPEN_PROTECTED = "OPEN_PROTECTED"
    PENDING_EXIT = "PENDING_EXIT"
    CLOSED = "CLOSED"

    @property
    def is_open(self) -> bool:
        return self in (
            TradeStatus.PENDING_ENTRY,
            TradeStatus.OPEN_UNPROTECTED,
            TradeStatus.OPEN_PROTECTED,
            TradeStatus.PENDING_EXIT,
        )


class TradeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    client_tag: str
    strategy_version_id: int
    config_hash: str
    session_id: str
    status: TradeStatus
    side: Side
    units: float
    signal_bar_ts: datetime
    decision_close: float
    opened_at: datetime
    entry_order_id: str | None = None
    entry_fill_price: float | None = None
    entry_slippage_bps: float | None = None
    sl: float
    tp: float
    sl_distance: float
    sl_order_id: str | None = None
    indicators: dict[str, float] = Field(default_factory=dict)
    closed_at: datetime | None = None
    exit_order_id: str | None = None
    exit_price: float | None = None
    exit_reason: ExitReason | None = None
    pnl_usd: float | None = None
    pnl_r: float | None = None
    fees_usd: float | None = None
    fees_estimated: bool = True
    mfe: float | None = None
    mae: float | None = None
    mfe_r: float | None = None
    mae_r: float | None = None
    bars_held: int | None = None
    journal_entry_open_id: int | None = None
    journal_entry_close_id: int | None = None
    review: str | None = None

    @property
    def is_loss(self) -> bool:
        return (self.pnl_usd or 0.0) < 0.0


def _row(r: sqlite3.Row) -> TradeRecord:
    return TradeRecord(
        id=int(r["id"]),
        client_tag=str(r["client_tag"]),
        strategy_version_id=int(r["strategy_version_id"]),
        config_hash=str(r["config_hash"]),
        session_id=str(r["session_id"]),
        status=TradeStatus(r["status"]),
        side=Side(r["side"]),
        units=float(r["units"]),
        signal_bar_ts=require_iso(r["signal_bar_ts"], field="signal_bar_ts"),
        decision_close=float(r["decision_close"]),
        opened_at=require_iso(r["opened_at"], field="opened_at"),
        entry_order_id=r["entry_order_id"],
        entry_fill_price=r["entry_fill_price"],
        entry_slippage_bps=r["entry_slippage_bps"],
        sl=float(r["sl"]),
        tp=float(r["tp"]),
        sl_distance=float(r["sl_distance"]),
        sl_order_id=r["sl_order_id"],
        indicators=dict(json.loads(r["indicators"] or "{}")),
        closed_at=from_iso(r["closed_at"]),
        exit_order_id=r["exit_order_id"],
        exit_price=r["exit_price"],
        exit_reason=ExitReason(r["exit_reason"]) if r["exit_reason"] else None,
        pnl_usd=r["pnl_usd"],
        pnl_r=r["pnl_r"],
        fees_usd=r["fees_usd"],
        fees_estimated=bool(r["fees_estimated"]),
        mfe=r["mfe"],
        mae=r["mae"],
        mfe_r=r["mfe_r"],
        mae_r=r["mae_r"],
        bars_held=r["bars_held"],
        journal_entry_open_id=r["journal_entry_open_id"],
        journal_entry_close_id=r["journal_entry_close_id"],
        review=r["review"],
    )


def insert_open(
    conn: sqlite3.Connection,
    *,
    client_tag: str,
    strategy_version_id: int,
    config_hash: str,
    session_id: str,
    status: TradeStatus,
    side: Side,
    units: float,
    signal_bar_ts: datetime,
    decision_close: float,
    opened_at: datetime,
    entry_order_id: str | None,
    entry_fill_price: float | None,
    entry_slippage_bps: float | None,
    sl: float,
    tp: float,
    sl_distance: float,
    sl_order_id: str | None,
    indicators: dict[str, float],
) -> int:
    cur = conn.execute(
        "INSERT INTO trades(client_tag, strategy_version_id, config_hash, session_id,"
        " status, side, units, signal_bar_ts, decision_close, opened_at,"
        " entry_order_id, entry_fill_price, entry_slippage_bps, sl, tp, sl_distance,"
        " sl_order_id, indicators)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            client_tag,
            strategy_version_id,
            config_hash,
            session_id,
            status.value,
            side.value,
            units,
            to_iso(signal_bar_ts),
            decision_close,
            to_iso(opened_at),
            entry_order_id,
            entry_fill_price,
            entry_slippage_bps,
            sl,
            tp,
            sl_distance,
            sl_order_id,
            json.dumps(indicators, sort_keys=True),
        ),
    )
    return int(cur.lastrowid or 0)


def set_status(
    conn: sqlite3.Connection,
    trade_id: int,
    status: TradeStatus,
    *,
    sl_order_id: str | None = None,
) -> None:
    if sl_order_id is None:
        conn.execute(
            "UPDATE trades SET status = ? WHERE id = ?", (status.value, trade_id)
        )
    else:
        conn.execute(
            "UPDATE trades SET status = ?, sl_order_id = ? WHERE id = ?",
            (status.value, sl_order_id, trade_id),
        )


def set_journal_open_id(conn: sqlite3.Connection, trade_id: int, journal_id: int) -> None:
    conn.execute(
        "UPDATE trades SET journal_entry_open_id = ? WHERE id = ?", (journal_id, trade_id)
    )


def set_journal_close_id(conn: sqlite3.Connection, trade_id: int, journal_id: int) -> None:
    # Allowed after close: the immutability trigger only guards the numbers.
    conn.execute(
        "UPDATE trades SET journal_entry_close_id = ? WHERE id = ?",
        (journal_id, trade_id),
    )


def mark_closed(
    conn: sqlite3.Connection,
    trade_id: int,
    *,
    closed_at: datetime,
    exit_order_id: str | None,
    exit_price: float,
    exit_reason: ExitReason,
    pnl_usd: float,
    pnl_r: float,
    fees_usd: float,
    fees_estimated: bool,
    mfe: float,
    mae: float,
    mfe_r: float,
    mae_r: float,
    bars_held: int,
) -> None:
    conn.execute(
        "UPDATE trades SET status = ?, closed_at = ?, exit_order_id = ?, exit_price = ?,"
        " exit_reason = ?, pnl_usd = ?, pnl_r = ?, fees_usd = ?, fees_estimated = ?,"
        " mfe = ?, mae = ?, mfe_r = ?, mae_r = ?, bars_held = ?"
        " WHERE id = ?",
        (
            TradeStatus.CLOSED.value,
            to_iso(closed_at),
            exit_order_id,
            exit_price,
            exit_reason.value,
            pnl_usd,
            pnl_r,
            fees_usd,
            1 if fees_estimated else 0,
            mfe,
            mae,
            mfe_r,
            mae_r,
            bars_held,
            trade_id,
        ),
    )


def attach_review(conn: sqlite3.Connection, trade_id: int, review: str) -> None:
    conn.execute("UPDATE trades SET review = ? WHERE id = ?", (review, trade_id))


def get(conn: sqlite3.Connection, trade_id: int) -> TradeRecord | None:
    r = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return _row(r) if r else None


def get_by_tag(conn: sqlite3.Connection, client_tag: str) -> TradeRecord | None:
    r = conn.execute(
        "SELECT * FROM trades WHERE client_tag = ?", (client_tag,)
    ).fetchone()
    return _row(r) if r else None


def open_trades(conn: sqlite3.Connection) -> list[TradeRecord]:
    rows = conn.execute(
        "SELECT * FROM trades WHERE status != ? ORDER BY id ASC",
        (TradeStatus.CLOSED.value,),
    ).fetchall()
    return [_row(r) for r in rows]


def open_trade(conn: sqlite3.Connection) -> TradeRecord | None:
    """The single open trade, if any. The system never runs more than one."""
    rows = open_trades(conn)
    return rows[-1] if rows else None


def list_closed(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[TradeRecord]:
    sql = "SELECT * FROM trades WHERE status = ?"
    params: list[Any] = [TradeStatus.CLOSED.value]
    if since is not None:
        sql += " AND closed_at >= ?"
        params.append(to_iso(since))
    if until is not None:
        sql += " AND closed_at < ?"
        params.append(to_iso(until))
    sql += " ORDER BY closed_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row(r) for r in rows]


def list_recent(conn: sqlite3.Connection, *, limit: int = 50) -> list[TradeRecord]:
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row(r) for r in rows]


def closed_on_day(conn: sqlite3.Connection, day: date) -> list[TradeRecord]:
    rows = conn.execute(
        "SELECT * FROM trades WHERE status = ? AND substr(closed_at, 1, 10) = ?"
        " ORDER BY closed_at ASC",
        (TradeStatus.CLOSED.value, day.isoformat()),
    ).fetchall()
    return [_row(r) for r in rows]


def opened_on_day(conn: sqlite3.Connection, day: date) -> list[TradeRecord]:
    rows = conn.execute(
        "SELECT * FROM trades WHERE substr(opened_at, 1, 10) = ? ORDER BY opened_at ASC",
        (day.isoformat(),),
    ).fetchall()
    return [_row(r) for r in rows]


def realized_pnl_between(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> float:
    r = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0.0) AS pnl FROM trades"
        " WHERE status = ? AND closed_at >= ? AND closed_at < ?",
        (TradeStatus.CLOSED.value, to_iso(start), to_iso(end)),
    ).fetchone()
    return float(r["pnl"]) if r else 0.0


def summarize(trades: Sequence[TradeRecord]) -> dict[str, float]:
    """Aggregate stats over closed trades, for reports and the LLM prompt."""
    closed = [t for t in trades if t.status is TradeStatus.CLOSED and t.pnl_r is not None]
    if not closed:
        return {
            "count": 0.0,
            "net_pnl_usd": 0.0,
            "expectancy_r": 0.0,
            "win_rate": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "profit_factor": 0.0,
        }
    rs = [float(t.pnl_r or 0.0) for t in closed]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "count": float(len(closed)),
        "net_pnl_usd": float(sum(float(t.pnl_usd or 0.0) for t in closed)),
        "expectancy_r": float(sum(rs) / len(rs)),
        "win_rate": float(len(wins) / len(rs)),
        "avg_win_r": float(gross_win / len(wins)) if wins else 0.0,
        "avg_loss_r": float(-gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else 0.0,
    }
