"""Candle storage.

Bars are written once and read many times: the backtester, the walk-forward
folds and the shadow runner all read from here, so the live loop and every
evaluation see byte-identical history.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime

import pandas as pd

from app.broker.types import Bar

from .sqlite import require_iso, to_iso


def upsert_bars(
    conn: sqlite3.Connection,
    instrument: str,
    granularity: str,
    bars: Sequence[Bar],
) -> int:
    """Insert or replace bars. Returns the number of rows written."""
    if not bars:
        return 0
    rows = [
        (instrument, granularity, to_iso(b.ts), b.o, b.h, b.l, b.c, b.v) for b in bars
    ]
    conn.executemany(
        "INSERT INTO candles(instrument, granularity, ts, o, h, l, c, v)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(instrument, granularity, ts) DO UPDATE SET"
        " o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, v=excluded.v",
        rows,
    )
    return len(rows)


def last_ts(
    conn: sqlite3.Connection, instrument: str, granularity: str
) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(ts) AS ts FROM candles WHERE instrument = ? AND granularity = ?",
        (instrument, granularity),
    ).fetchone()
    if row is None or row["ts"] is None:
        return None
    return require_iso(row["ts"], field="ts")


def count(conn: sqlite3.Connection, instrument: str, granularity: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM candles WHERE instrument = ? AND granularity = ?",
        (instrument, granularity),
    ).fetchone()
    return int(row["n"]) if row else 0


def read_bars(
    conn: sqlite3.Connection,
    instrument: str,
    granularity: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """Return an ascending DataFrame indexed by UTC timestamp with o/h/l/c/v.

    When `limit` is given the *most recent* `limit` bars are returned, still
    in ascending order — that is what the live loop wants.
    """
    sql = "SELECT ts, o, h, l, c, v FROM candles WHERE instrument = ? AND granularity = ?"
    params: list[object] = [instrument, granularity]
    if start is not None:
        sql += " AND ts >= ?"
        params.append(to_iso(start))
    if end is not None:
        sql += " AND ts <= ?"
        params.append(to_iso(end))
    if limit is not None:
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
    else:
        sql += " ORDER BY ts ASC"

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return empty_frame()
    records = [
        {
            "ts": pd.Timestamp(r["ts"]),
            "o": float(r["o"]),
            "h": float(r["h"]),
            "l": float(r["l"]),
            "c": float(r["c"]),
            "v": float(r["v"]),
        }
        for r in rows
    ]
    df = pd.DataFrame.from_records(records).set_index("ts").sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def empty_frame() -> pd.DataFrame:
    """An empty candle frame with the right dtypes and a tz-aware index."""
    idx = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in ("o", "h", "l", "c", "v")}, index=idx
    )
