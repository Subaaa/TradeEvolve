"""Storage guarantees: append-only journal, one champion, derived counters."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.db import candles as candles_db
from app.db import strategy_versions as sv_db
from app.db import system_state as ss
from app.db import trades as trades_db
from app.db.journal_store import SqliteJournalStore
from app.db.strategy_versions import VersionStatus
from app.db.trades import TradeStatus
from app.journal.hash_chain import verify_chain
from app.journal.payloads import SystemEventPayload
from app.journal.types import JournalKind
from app.journal.writer import JournalWriter
from app.risk.account_state import build_from_db
from app.risk.types import ExitReason, Side
from app.strategy.configs import StrategyConfig
from pinned.risk_limits import RISK_LIMITS
from test.conftest import M15, START, make_bar

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- the journal


def test_the_journal_cannot_be_updated(
    conn: sqlite3.Connection, journal: JournalWriter
) -> None:
    entry = journal.write(
        kind=JournalKind.SYSTEM_EVENT, payload=SystemEventPayload(event="x")
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE journal_entries SET payload = '{}' WHERE id = ?", (entry.id,)
        )


def test_the_journal_cannot_be_deleted(
    conn: sqlite3.Connection, journal: JournalWriter
) -> None:
    entry = journal.write(
        kind=JournalKind.SYSTEM_EVENT, payload=SystemEventPayload(event="x")
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM journal_entries WHERE id = ?", (entry.id,))


def test_the_hash_chain_verifies_across_a_round_trip(
    conn: sqlite3.Connection, journal: JournalWriter
) -> None:
    for i in range(5):
        journal.write(
            kind=JournalKind.SYSTEM_EVENT,
            payload=SystemEventPayload(event=f"e{i}", data={"i": i}),
        )
    store = SqliteJournalStore(conn)
    ok, bad = verify_chain(store.list_for_session("test-session"))
    assert ok is True
    assert bad is None


def test_reason_codes_are_counted_for_the_review(
    conn: sqlite3.Connection, journal: JournalWriter
) -> None:
    journal.write(
        kind=JournalKind.TRADE_DECISION,
        payload={"x": 1},
        reason_codes=["MAX_TRADES_PER_DAY"],
    )
    journal.write(
        kind=JournalKind.TRADE_DECISION,
        payload={"x": 2},
        reason_codes=["MAX_TRADES_PER_DAY", "STALE_DATA"],
    )
    counts = SqliteJournalStore(conn).count_by_reason_since("2000-01-01T00:00:00+00:00")
    assert counts["MAX_TRADES_PER_DAY"] == 2
    assert counts["STALE_DATA"] == 1


# ------------------------------------------------------------ strategy versions


def test_only_one_champion_can_exist(
    conn: sqlite3.Connection, config: StrategyConfig
) -> None:
    sv_db.insert_version(conn, config, status=VersionStatus.CHAMPION, created_by="t")
    other = config.model_copy(update={"version_tag": "other"})
    with pytest.raises(sqlite3.IntegrityError):
        sv_db.insert_version(conn, other, status=VersionStatus.CHAMPION, created_by="t")


def test_promotion_retires_the_previous_champion(
    conn: sqlite3.Connection, config: StrategyConfig
) -> None:
    old = sv_db.insert_version(
        conn, config, status=VersionStatus.CHAMPION, created_by="t"
    )
    challenger = config.model_copy(update={"version_tag": "chal", "tp_r_multiple": 2.5})
    new = sv_db.insert_version(
        conn, challenger, status=VersionStatus.SHADOW, created_by="llm"
    )

    sv_db.promote(conn, challenger_id=new)

    champion = sv_db.load_champion(conn)
    assert champion is not None
    assert champion.id == new
    assert champion.parent_version_id == old
    retired = sv_db.get(conn, old)
    assert retired is not None and retired.status is VersionStatus.RETIRED


def test_ensure_champion_is_idempotent(
    conn: sqlite3.Connection, config: StrategyConfig
) -> None:
    first = sv_db.ensure_champion(conn, config)
    second = sv_db.ensure_champion(conn, config)
    assert first.id == second.id


def test_the_config_hash_ignores_the_version_tag(config: StrategyConfig) -> None:
    """Two versions with identical behaviour must hash identically."""
    renamed = config.model_copy(update={"version_tag": "different_name"})
    assert config.canonical_hash_hex() == renamed.canonical_hash_hex()
    changed = config.model_copy(update={"tp_r_multiple": 3.0})
    assert config.canonical_hash_hex() != changed.canonical_hash_hex()


# ------------------------------------------------------------------- candles


def test_candles_round_trip_with_fractional_volume(conn: sqlite3.Connection) -> None:
    bars = [make_bar(START + i * M15, 1, 2, 0.5, 1.5, v=0.0447) for i in range(3)]
    candles_db.upsert_bars(conn, "XAU_USD", "M15", bars)
    df = candles_db.read_bars(conn, "XAU_USD", "M15")
    assert len(df) == 3
    assert df["v"].iloc[0] == pytest.approx(0.0447)
    assert df.index.tz is not None


def test_candle_upsert_is_idempotent(conn: sqlite3.Connection) -> None:
    bars = [make_bar(START, 1, 2, 0.5, 1.5)]
    candles_db.upsert_bars(conn, "XAU_USD", "M15", bars)
    candles_db.upsert_bars(conn, "XAU_USD", "M15", bars)
    assert candles_db.count(conn, "XAU_USD", "M15") == 1


def test_read_bars_with_a_limit_returns_the_most_recent_ascending(
    conn: sqlite3.Connection,
) -> None:
    bars = [make_bar(START + i * M15, 1, 2, 0.5, float(i)) for i in range(10)]
    candles_db.upsert_bars(conn, "XAU_USD", "M15", bars)
    df = candles_db.read_bars(conn, "XAU_USD", "M15", limit=3)
    assert list(df["c"]) == [7.0, 8.0, 9.0]


# --------------------------------------------------------------- account state


def _insert_closed(
    conn: sqlite3.Connection,
    champion_id: int,
    *,
    tag: str,
    opened: datetime,
    closed: datetime,
    pnl: float,
    exit_reason: ExitReason = ExitReason.SL,
) -> int:
    tid = trades_db.insert_open(
        conn,
        client_tag=tag,
        strategy_version_id=champion_id,
        config_hash="h",
        session_id="s",
        status=TradeStatus.OPEN_PROTECTED,
        side=Side.LONG,
        units=0.05,
        signal_bar_ts=opened,
        decision_close=4_000.0,
        opened_at=opened,
        entry_order_id="e",
        entry_fill_price=4_000.0,
        entry_slippage_bps=0.0,
        sl=3_960.0,
        tp=4_080.0,
        sl_distance=40.0,
        sl_order_id="s1",
        indicators={},
    )
    trades_db.mark_closed(
        conn,
        tid,
        closed_at=closed,
        exit_order_id="x",
        exit_price=3_960.0 if pnl < 0 else 4_080.0,
        exit_reason=exit_reason,
        pnl_usd=pnl,
        pnl_r=pnl / 2.0,
        fees_usd=1.0,
        fees_estimated=True,
        mfe=1.0,
        mae=1.0,
        mfe_r=0.5,
        mae_r=0.5,
        bars_held=4,
    )
    return tid


def test_account_state_is_derived_from_the_trades_table(
    conn: sqlite3.Connection, champion_id: int
) -> None:
    """The counters that drive every discipline gate are a query, not state.

    The old implementation kept them in memory and never updated them, so the
    daily-loss and cooldown gates could not fire at all.
    """
    now = datetime(2026, 3, 3, 18, 0, tzinfo=UTC)
    day_start = now.replace(hour=0, minute=0)

    _insert_closed(
        conn, champion_id, tag="t1",
        opened=day_start + timedelta(hours=1),
        closed=day_start + timedelta(hours=2),
        pnl=-50.0,
    )
    _insert_closed(
        conn, champion_id, tag="t2",
        opened=day_start + timedelta(hours=3),
        closed=day_start + timedelta(hours=4),
        pnl=-30.0,
    )

    state = build_from_db(
        conn,
        equity=9_920.0,
        open_positions=0,
        now=now,
        limits=RISK_LIMITS,
        granularity="M15",
    )

    assert state.trades_today == 2
    assert state.losses_today == 2
    assert state.day_pnl == pytest.approx(-80.0)
    assert state.consecutive_losses == 2
    assert state.last_exit_reason is ExitReason.SL
    assert state.last_exit_was_loss is True
    assert state.bars_since_last_exit is not None and state.bars_since_last_exit > 0


def test_a_win_resets_the_consecutive_loss_counter(
    conn: sqlite3.Connection, champion_id: int
) -> None:
    now = datetime(2026, 3, 3, 18, 0, tzinfo=UTC)
    base = now.replace(hour=1, minute=0)
    _insert_closed(conn, champion_id, tag="a", opened=base, closed=base + M15, pnl=-10.0)
    _insert_closed(
        conn, champion_id, tag="b",
        opened=base + 2 * M15, closed=base + 3 * M15, pnl=-10.0,
    )
    _insert_closed(
        conn, champion_id, tag="c",
        opened=base + 4 * M15, closed=base + 5 * M15,
        pnl=25.0, exit_reason=ExitReason.TP,
    )

    state = build_from_db(
        conn, equity=10_005.0, open_positions=0, now=now,
        limits=RISK_LIMITS, granularity="M15",
    )
    assert state.consecutive_losses == 0
    assert state.last_exit_was_loss is False
    assert state.last_exit_reason is ExitReason.TP


def test_the_day_anchor_survives_an_equity_swing(conn: sqlite3.Connection) -> None:
    now = datetime(2026, 3, 3, 8, 0, tzinfo=UTC)
    day, _ = ss.ensure_anchors(conn, equity=10_000.0, now=now)
    assert day.start_equity == 10_000.0
    # Later the same day, a big win must not move the anchor.
    later, _ = ss.ensure_anchors(conn, equity=12_000.0, now=now + timedelta(hours=6))
    assert later.start_equity == 10_000.0
    # The next UTC day re-anchors.
    tomorrow, _ = ss.ensure_anchors(conn, equity=12_000.0, now=now + timedelta(days=1))
    assert tomorrow.start_equity == 12_000.0


def test_equity_high_only_ratchets_up(conn: sqlite3.Connection) -> None:
    assert ss.bump_equity_high(conn, 10_000.0) == 10_000.0
    assert ss.bump_equity_high(conn, 11_000.0) == 11_000.0
    assert ss.bump_equity_high(conn, 9_000.0) == 11_000.0


def test_trades_cannot_be_deleted(
    conn: sqlite3.Connection, champion_id: int
) -> None:
    now = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)
    tid = _insert_closed(conn, champion_id, tag="d", opened=now, closed=now + M15, pnl=-1.0)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM trades WHERE id = ?", (tid,))
