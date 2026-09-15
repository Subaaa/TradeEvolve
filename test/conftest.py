"""Shared fixtures: an in-memory database and a synthetic M15 series."""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.broker.fake import FakeBroker
from app.broker.types import Bar
from app.db import strategy_versions as sv_db
from app.db.journal_store import SqliteJournalStore
from app.db.sqlite import connect, migrate
from app.db.strategy_versions import VersionStatus
from app.journal.writer import JournalWriter
from app.strategy.configs import StrategyConfig

M15 = timedelta(minutes=15)
START = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)  # a Monday


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Never let a developer's `.env` decide what the tests do.

    Without this, a stale `GRANULARITY` or a real API key in the working
    directory silently changes what is being tested — which is exactly how a
    suite starts passing on one machine and failing on another.
    """
    from app.config import get_settings

    monkeypatch.setenv("NITO_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("INSTRUMENT", "XAU_USD")
    monkeypatch.setenv("GRANULARITY", "M15")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ALPACA_WRITE_ENABLED", "false")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.sqlite"))
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = connect(":memory:")
    migrate(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def config() -> StrategyConfig:
    return StrategyConfig(version_tag="test_v1")


@pytest.fixture
def champion_id(conn: sqlite3.Connection, config: StrategyConfig) -> int:
    return sv_db.insert_version(
        conn, config, status=VersionStatus.CHAMPION, created_by="test"
    )


@pytest.fixture
def journal(conn: sqlite3.Connection) -> JournalWriter:
    return JournalWriter(SqliteJournalStore(conn), session_id="test-session")


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker(equity=10_000.0, price=4_000.0)


def make_bar(  # noqa: PLR0917 - an OHLCV bar reads best positionally
    ts: datetime, o: float, h: float, low: float, c: float, v: float = 1.0
) -> Bar:
    return Bar(ts=ts, o=o, h=h, l=low, c=c, v=v)


def flat_series(n: int, *, price: float = 4_000.0, start: datetime = START) -> pd.DataFrame:
    """A perfectly flat series: no trend, no signal, usable as a base."""
    rows = [
        {
            "ts": start + i * M15,
            "o": price,
            "h": price + 0.5,
            "l": price - 0.5,
            "c": price,
            "v": 1.0,
        }
        for i in range(n)
    ]
    return pd.DataFrame.from_records(rows).set_index("ts").sort_index()


def pullback_series(
    *, warmup: int = 260, start: datetime = START, base: float = 400.0
) -> pd.DataFrame:
    """A series that produces exactly one entry signal on the final bar.

    Built in four phases so every clause of the rule is exercised. Three
    details are deliberate rather than cosmetic:

    *   Bar ranges are **wide early and calmer later**. A ramp with ranges
        proportional to price makes absolute ATR strictly increasing, which
        pins the ATR percentile at 1.0 and trips the volatility-spike filter —
        correct behaviour from the rule, useless as a fixture.
    *   A short rally precedes the pullback, so the three down bars dip to the
        fast EMA without dragging it back under the slow one.
    *   `base` is low relative to the bar ranges, so the resulting ATR is a
        large fraction of price and the stop clears
        `RISK_LIMITS.min_sl_distance_bps`. A fixture that cannot pay its own
        costs would be refused by the engine, which is correct behaviour and
        a useless test.
    """
    rows: list[dict[str, float | datetime]] = []
    ts = start

    def bar(price: float, span: float, *, close: float | None = None) -> None:
        nonlocal ts
        c = price if close is None else close
        rows.append(
            {
                "ts": ts,
                "o": price - span * 0.2,
                "h": max(price, c) + span,
                "l": min(price, c) - span,
                "c": c,
                "v": 1.0,
            }
        )
        ts += M15

    # Phase 1: a choppy, gently drifting market that settles down.
    for i in range(warmup):
        phase = i / warmup
        price = base + 30.0 * phase + 18.0 * math.sin(i / 5.0) * (1.0 - 0.7 * phase)
        bar(price, 14.0 * (1.0 - 0.75 * phase))

    # Phase 2: a short rally, putting the fast EMA clearly above the slow one.
    price = float(rows[-1]["c"])
    for _ in range(10):
        price += 5.0
        bar(price, 4.0)

    # Phase 3: three down bars that dip to the fast EMA.
    for _ in range(3):
        price -= 6.0
        bar(price, 4.5)

    # Phase 4: the resumption bar, closing above the previous bar's high.
    prior_high = float(rows[-1]["h"])
    bar(price, 1.0, close=prior_high + 9.0)

    return pd.DataFrame.from_records(rows).set_index("ts").sort_index()
