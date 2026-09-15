"""End-to-end smoke tests: the tick, the API, and the CLI wiring."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.broker.fake import FakeBroker
from app.broker.types import Bar
from app.db import candles as candles_db
from app.db import strategy_versions as sv_db
from app.db import system_state as ss
from app.db import trades as trades_db
from app.db.journal_store import SqliteJournalStore
from app.db.strategy_versions import VersionStatus
from app.execution.position_manager import PositionManager
from app.journal.writer import JournalWriter
from app.risk import kill_switch
from app.scheduler import deps
from app.scheduler.cron import _bar_close_minutes, build_scheduler
from app.strategy.configs import StrategyConfig
from test.conftest import M15, pullback_series

pytestmark = pytest.mark.unit


@pytest.fixture
def wired(
    conn: sqlite3.Connection, broker: FakeBroker, config: StrategyConfig
) -> Iterator[tuple[sqlite3.Connection, FakeBroker]]:
    """Point the shared dependency cache at the test doubles."""
    journal = JournalWriter(SqliteJournalStore(conn), session_id="smoke")
    sv_db.ensure_champion(conn, config)
    deps.reset()
    deps.db.cache_clear()
    deps.db.__wrapped__ = lambda: conn  # type: ignore[attr-defined]

    manager = PositionManager(
        broker=broker,
        conn=conn,
        journal=journal,
        instrument="XAU_USD",
        granularity="M15",
        poll_attempts=1,
        poll_interval_sec=0.0,
    )
    originals = (deps.db, deps.broker, deps.journal, deps.position_manager)
    deps.db = lambda: conn  # type: ignore[assignment]
    deps.broker = lambda: broker  # type: ignore[assignment]
    deps.journal = lambda: journal  # type: ignore[assignment]
    deps.position_manager = lambda: manager  # type: ignore[assignment]
    try:
        yield conn, broker
    finally:
        deps.db, deps.broker, deps.journal, deps.position_manager = originals  # type: ignore[assignment]
        deps.reset()


# ------------------------------------------------------------------ the tick


def _seed_broker(broker: FakeBroker) -> datetime:
    """Load the signal-producing series into the fake broker."""
    df = pullback_series()
    for ts, r in df.iterrows():
        broker._bars.append(
            Bar(
                ts=ts.to_pydatetime(),
                o=float(r["o"]),
                h=float(r["h"]),
                l=float(r["l"]),
                c=float(r["c"]),
                v=float(r["v"]),
            )
        )
    last = df.index[-1].to_pydatetime()
    broker.set_price(float(df["c"].iloc[-1]))
    broker.now = last + M15
    return last


def test_a_tick_on_a_signal_bar_opens_a_protected_position(
    wired: tuple[sqlite3.Connection, FakeBroker],
) -> None:
    from app.scheduler.jobs import tick_bar_close

    conn, broker = wired
    last = _seed_broker(broker)

    tick_bar_close._tick(last + M15)

    trade = trades_db.open_trade(conn)
    assert trade is not None
    assert trade.sl_order_id is not None
    assert broker.get_order(trade.sl_order_id).status.is_working
    assert ss.load_last_processed_bar_ts(conn) == last


def test_the_same_bar_is_never_traded_twice(
    wired: tuple[sqlite3.Connection, FakeBroker],
) -> None:
    from app.scheduler.jobs import tick_bar_close

    conn, broker = wired
    last = _seed_broker(broker)

    tick_bar_close._tick(last + M15)
    before = len(trades_db.list_recent(conn, limit=50))
    # A second tick inside the same bar must be a no-op.
    tick_bar_close._tick(last + M15 + timedelta(seconds=30))
    assert len(trades_db.list_recent(conn, limit=50)) == before


def test_a_tick_with_the_kill_switch_engaged_opens_nothing(
    wired: tuple[sqlite3.Connection, FakeBroker],
) -> None:
    from app.scheduler.jobs import tick_bar_close

    conn, broker = wired
    last = _seed_broker(broker)
    kill_switch.engage(conn, reason="manual", requires_manual_clear=True)

    tick_bar_close._tick(last + M15)

    assert trades_db.open_trade(conn) is None
    rows = conn.execute(
        "SELECT reason_codes FROM journal_entries WHERE kind = 'trade_decision'"
    ).fetchall()
    assert any("KILL_SWITCH_ENGAGED" in r["reason_codes"] for r in rows)


def test_a_tick_records_a_no_signal_decision(
    wired: tuple[sqlite3.Connection, FakeBroker],
) -> None:
    from app.scheduler.jobs import tick_bar_close
    from test.conftest import flat_series

    conn, broker = wired
    df = flat_series(400)
    for ts, r in df.iterrows():
        broker._bars.append(
            Bar(
                ts=ts.to_pydatetime(),
                o=float(r["o"]),
                h=float(r["h"]),
                l=float(r["l"]),
                c=float(r["c"]),
                v=float(r["v"]),
            )
        )
    last = df.index[-1].to_pydatetime()
    broker.now = last + M15

    tick_bar_close._tick(last + M15)

    assert trades_db.open_trade(conn) is None
    rows = conn.execute(
        "SELECT next_state FROM journal_entries WHERE kind = 'trade_decision'"
    ).fetchall()
    assert any(r["next_state"] == "NO_SIGNAL" for r in rows)


def test_the_tick_never_raises(wired: tuple[sqlite3.Connection, FakeBroker]) -> None:
    """A raising job kills the scheduler thread; a logged one does not."""
    from app.scheduler.jobs import tick_bar_close, tick_monitor

    tick_bar_close.run()
    tick_monitor.run()


# -------------------------------------------------------------------- the API


def test_the_api_serves_every_route(
    wired: tuple[sqlite3.Connection, FakeBroker],
) -> None:
    client = TestClient(create_app())

    assert client.get("/health").json()["ok"] is True

    state = client.get("/system/state").json()
    assert state["kill_switch"]["engaged"] is False

    assert client.get("/trades").json()["items"] == []
    assert client.get("/journal").json()["items"] == []

    strategy = client.get("/strategy/current").json()
    assert strategy["champion"]["version_tag"] == "test_v1"
    assert strategy["active_experiment"] is None

    assert client.get("/reports/daily").json()["items"] == []


def test_the_api_reflects_an_open_trade(
    wired: tuple[sqlite3.Connection, FakeBroker],
) -> None:
    from app.scheduler.jobs import tick_bar_close

    _, broker = wired
    last = _seed_broker(broker)
    tick_bar_close._tick(last + M15)

    client = TestClient(create_app())
    body = client.get("/system/state").json()
    assert body["open_trade"] is not None
    assert body["last_processed_bar_ts"] is not None

    trades = client.get("/trades").json()
    assert len(trades["items"]) == 1


def test_the_api_is_read_only() -> None:
    """None of the routes accept a write verb."""
    client = TestClient(create_app())
    for path in ("/system/state", "/trades", "/journal", "/strategy/current"):
        assert client.post(path).status_code == 405


# -------------------------------------------------------------- the scheduler


def test_the_bar_close_schedule_matches_the_granularity() -> None:
    assert _bar_close_minutes("M15") == "0,15,30,45"
    assert _bar_close_minutes("M5") == "0,5,10,15,20,25,30,35,40,45,50,55"
    assert _bar_close_minutes("H1") == "0"


def test_every_job_is_registered_once() -> None:
    # `build_scheduler` returns an unstarted scheduler, so there is nothing
    # to shut down afterwards.
    sched = build_scheduler("M15")
    ids = sorted(j.id for j in sched.get_jobs())
    assert ids == [
        "audit_verify",
        "daily_review",
        "daily_rollover",
        "shadow_update",
        "tick_bar_close",
        "tick_monitor",
        "weekly_evolution",
    ]
    for job in sched.get_jobs():
        assert job.max_instances == 1
        assert job.coalesce is True


# --------------------------------------------------------------------- the CLI


def test_the_cli_parser_exposes_the_operator_commands() -> None:
    from app.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["kill-switch", "clear"])
    assert args.action == "clear"
    args = parser.parse_args(["evolve", "--dry-run"])
    assert args.dry_run is True


def test_the_backtest_command_runs_against_stored_candles(
    conn: sqlite3.Connection, config: StrategyConfig, tmp_path: object
) -> None:
    from app.evolution.backtester import BacktestInput, run

    df = pullback_series()
    candles_db.upsert_bars(
        conn,
        "XAU_USD",
        "M15",
        [
            Bar(
                ts=ts.to_pydatetime(),
                o=float(r["o"]),
                h=float(r["h"]),
                l=float(r["l"]),
                c=float(r["c"]),
                v=float(r["v"]),
            )
            for ts, r in df.iterrows()
        ],
    )
    stored = candles_db.read_bars(conn, "XAU_USD", "M15")
    out = run(
        BacktestInput(
            config=config,
            candles=stored,
            start=stored.index[0].to_pydatetime(),
            end=stored.index[-1].to_pydatetime(),
            initial_equity=10_000.0,
        )
    )
    assert out.config_hash
    assert out.metrics.trade_count >= 0


def test_a_fresh_database_boots_with_a_champion(
    conn: sqlite3.Connection, config: StrategyConfig
) -> None:
    champion = sv_db.ensure_champion(conn, config)
    assert champion.status is VersionStatus.CHAMPION
    assert sv_db.load_champion(conn) is not None
    assert datetime.now(tz=UTC) > champion.created_at - timedelta(seconds=5)
