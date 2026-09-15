"""Position lifecycle, including every way it can go wrong.

The property under test throughout: a position either has a broker-resident
stop, or it does not exist. The previous system violated that on every single
trade, so these tests are the regression suite for the whole rebuild.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from app.broker.fake import FakeBroker
from app.broker.types import BrokerOrderType, OrderRequest, OrderSide, OrderStatus
from app.db import trades as trades_db
from app.db.trades import TradeStatus
from app.execution.position_manager import PositionManager
from app.execution.tags import make_tag
from app.journal.writer import JournalWriter
from app.risk.types import ExitReason, OrderSpec, OrderType, Side, TimeInForce
from app.strategy.configs import StrategyConfig
from test.conftest import M15, make_bar

pytestmark = pytest.mark.unit

NOW = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)
ENTRY = 4_000.0
SL_DISTANCE = 40.0


@pytest.fixture
def manager(
    conn: sqlite3.Connection, broker: FakeBroker, journal: JournalWriter
) -> PositionManager:
    broker.now = NOW
    return PositionManager(
        broker=broker,
        conn=conn,
        journal=journal,
        instrument="XAU_USD",
        granularity="M15",
        poll_attempts=1,
        poll_interval_sec=0.0,
    )


def make_order(*, version_id: int, units: float = 0.05) -> OrderSpec:
    return OrderSpec(
        instrument="XAU_USD",
        side=Side.LONG,
        order_type=OrderType.MARKET,
        units=units,
        entry_price=ENTRY,
        stop_loss=ENTRY - SL_DISTANCE,
        take_profit=ENTRY + 2 * SL_DISTANCE,
        tif=TimeInForce.IOC,
        client_tag=make_tag(strategy_version_id=version_id, side=Side.LONG, bar_ts=NOW),
        expected_rr=2.0,
        bar_ts=NOW,
        strategy_version_id=version_id,
        indicators={"atr": 26.7},
    )


# ----------------------------------------------------------------------- open


def test_a_new_position_is_protected_immediately(
    manager: PositionManager,
    broker: FakeBroker,
    conn: sqlite3.Connection,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    trade = manager.open(make_order(version_id=champion_id), config=config, decision_close=ENTRY)

    assert trade is not None
    assert trade.status is TradeStatus.OPEN_PROTECTED
    assert trade.sl_order_id is not None

    resting = broker.get_order(trade.sl_order_id)
    assert resting.type is BrokerOrderType.STOP_LIMIT
    assert resting.side is OrderSide.SELL
    assert resting.status.is_working
    # The stop is a stop_limit because Alpaca crypto has no plain stop; the
    # limit must sit below the trigger or a fast move leaves us naked.
    assert resting.limit_price is not None and resting.stop_price is not None
    assert resting.limit_price < resting.stop_price


def test_the_stop_is_anchored_to_the_actual_fill_not_the_intended_price(
    manager: PositionManager,
    broker: FakeBroker,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    broker.set_price(4_010.0)  # 10 dollars of slippage against us
    trade = manager.open(make_order(version_id=champion_id), config=config, decision_close=ENTRY)

    assert trade is not None
    assert trade.entry_fill_price == pytest.approx(4_010.0)
    # The planned RISK is what sizing was based on, so the distance is kept.
    assert trade.sl == pytest.approx(4_010.0 - SL_DISTANCE)
    assert trade.sl_distance == pytest.approx(SL_DISTANCE)


def test_a_position_that_cannot_be_protected_is_flattened(
    manager: PositionManager,
    broker: FakeBroker,
    conn: sqlite3.Connection,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    broker.fail_next_stop_order = True
    trade = manager.open(make_order(version_id=champion_id), config=config, decision_close=ENTRY)

    assert trade is not None
    assert trade.status is TradeStatus.CLOSED
    assert trade.exit_reason is ExitReason.UNPROTECTED
    assert broker.list_positions() == []


def test_a_rejected_entry_leaves_no_trade(
    manager: PositionManager,
    broker: FakeBroker,
    conn: sqlite3.Connection,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    broker.reject_next_entry = True
    assert manager.open(make_order(version_id=champion_id), config=config, decision_close=ENTRY) is None
    assert trades_db.open_trades(conn) == []


def test_excessive_slippage_is_journaled_but_the_trade_still_opens(
    manager: PositionManager,
    broker: FakeBroker,
    conn: sqlite3.Connection,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    broker.set_price(ENTRY * 1.01)  # 100 bps, far past the 15 bps threshold
    trade = manager.open(make_order(version_id=champion_id), config=config, decision_close=ENTRY)

    assert trade is not None
    assert trade.status is TradeStatus.OPEN_PROTECTED
    rows = conn.execute(
        "SELECT reason_codes FROM journal_entries WHERE kind = 'risk_event'"
    ).fetchall()
    assert any("ENTRY_SLIPPAGE_EXCEEDED" in r["reason_codes"] for r in rows)


# -------------------------------------------------------------------- monitor


def _open(manager: PositionManager, champion_id: int, config: StrategyConfig):  # type: ignore[no-untyped-def]
    trade = manager.open(
        make_order(version_id=champion_id), config=config, decision_close=ENTRY
    )
    assert trade is not None
    return trade


def test_a_stop_fill_closes_the_trade_as_a_loss(
    manager: PositionManager,
    broker: FakeBroker,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    trade = _open(manager, champion_id, config)
    stop = trade.sl

    broker.advance(make_bar(NOW + M15, ENTRY, ENTRY, stop - 5, stop - 2))
    closed = manager.monitor(now=NOW + M15, latest_bid=stop - 2)

    assert len(closed) == 1
    assert closed[0].exit_reason is ExitReason.SL
    assert closed[0].pnl_usd is not None and closed[0].pnl_usd < 0
    assert closed[0].pnl_r is not None and closed[0].pnl_r < 0


def test_reaching_the_target_cancels_the_stop_and_sells(
    manager: PositionManager,
    broker: FakeBroker,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    trade = _open(manager, champion_id, config)
    assert trade.sl_order_id is not None

    broker.set_price(trade.tp + 1)
    closed = manager.monitor(now=NOW + M15, latest_bid=trade.tp + 1)

    assert len(closed) == 1
    assert closed[0].exit_reason is ExitReason.TP
    assert closed[0].pnl_usd is not None and closed[0].pnl_usd > 0
    assert broker.get_order(trade.sl_order_id).status is OrderStatus.CANCELED
    assert broker.list_positions() == []


def test_the_time_stop_closes_a_trade_that_is_going_nowhere(
    manager: PositionManager,
    broker: FakeBroker,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    _open(manager, champion_id, config)
    later = NOW + config.time_stop_bars * M15
    broker.now = later

    closed = manager.monitor(now=later, latest_bid=ENTRY + 1)

    assert len(closed) == 1
    assert closed[0].exit_reason is ExitReason.TIME_STOP


def test_the_time_stop_spares_a_trade_that_is_working(
    manager: PositionManager,
    broker: FakeBroker,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    trade = _open(manager, champion_id, config)
    later = NOW + config.time_stop_bars * M15
    broker.now = later
    # Up 1R: past `time_stop_min_r`, so the idea is still working.
    working = trade.entry_fill_price or ENTRY
    assert manager.monitor(now=later, latest_bid=working + SL_DISTANCE) == []


def test_the_hard_time_cap_always_closes(
    manager: PositionManager,
    broker: FakeBroker,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    trade = _open(manager, champion_id, config)
    later = NOW + 200 * M15
    broker.now = later
    working = trade.entry_fill_price or ENTRY
    closed = manager.monitor(now=later, latest_bid=working + SL_DISTANCE * 1.9)
    assert len(closed) == 1
    assert closed[0].exit_reason is ExitReason.HARD_TIME_CAP


def test_the_kill_switch_flattens_an_open_position(
    manager: PositionManager, champion_id: int, config: StrategyConfig
) -> None:
    _open(manager, champion_id, config)
    closed = manager.monitor(now=NOW + M15, latest_bid=ENTRY, kill_switch=True)
    assert len(closed) == 1
    assert closed[0].exit_reason is ExitReason.KILL_SWITCH


def test_a_lost_stop_is_replaced_rather_than_left_naked(
    manager: PositionManager,
    broker: FakeBroker,
    conn: sqlite3.Connection,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    trade = _open(manager, champion_id, config)
    assert trade.sl_order_id is not None
    broker.cancel_order(trade.sl_order_id)  # the stop vanishes

    assert manager.monitor(now=NOW + M15, latest_bid=ENTRY) == []

    reloaded = trades_db.get(conn, trade.id)
    assert reloaded is not None
    assert reloaded.status is TradeStatus.OPEN_PROTECTED
    assert reloaded.sl_order_id != trade.sl_order_id
    assert broker.get_order(reloaded.sl_order_id or "").status.is_working


def test_the_failsafe_fires_when_price_trades_through_an_unfilled_stop(
    manager: PositionManager,
    broker: FakeBroker,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    trade = _open(manager, champion_id, config)
    through = trade.sl - 10

    # One sighting could be a stale quote, so nothing happens yet.
    assert manager.monitor(now=NOW + M15, latest_bid=through) == []
    # Two in a row is a broken stop.
    closed = manager.monitor(now=NOW + 2 * M15, latest_bid=through)
    assert len(closed) == 1
    assert closed[0].exit_reason is ExitReason.SL_FAILSAFE


def test_a_single_through_stop_sighting_does_not_latch(
    manager: PositionManager,
    broker: FakeBroker,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    trade = _open(manager, champion_id, config)
    assert manager.monitor(now=NOW + M15, latest_bid=trade.sl - 10) == []
    assert manager.monitor(now=NOW + 2 * M15, latest_bid=ENTRY) == []
    assert manager.monitor(now=NOW + 3 * M15, latest_bid=trade.sl - 10) == []


# ------------------------------------------------------------------ closing


def test_a_closed_trade_records_the_full_forensics(
    manager: PositionManager,
    broker: FakeBroker,
    conn: sqlite3.Connection,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    from app.market.bars import to_dataframe

    trade = _open(manager, champion_id, config)
    bars = to_dataframe(
        [
            make_bar(NOW, ENTRY, ENTRY + 30, ENTRY - 5, ENTRY + 20),
            make_bar(NOW + M15, ENTRY + 20, ENTRY + 60, ENTRY - 20, trade.tp + 1),
        ]
    )
    broker.set_price(trade.tp + 1)
    closed = manager.monitor(
        now=NOW + M15, latest_bid=trade.tp + 1, closed_bars=bars
    )

    t = closed[0]
    assert t.mfe is not None and t.mfe > 0
    assert t.mae is not None and t.mae > 0
    assert t.mfe_r is not None and t.mae_r is not None
    assert t.bars_held is not None and t.bars_held >= 1
    assert t.fees_usd is not None and t.fees_usd > 0
    assert t.fees_estimated is True
    assert t.journal_entry_close_id is not None

    payloads = conn.execute(
        "SELECT payload FROM journal_entries WHERE kind = 'trade_closed'"
    ).fetchall()
    assert len(payloads) == 1


def test_a_closed_trade_is_immutable(
    manager: PositionManager,
    broker: FakeBroker,
    conn: sqlite3.Connection,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    trade = _open(manager, champion_id, config)
    broker.set_price(trade.tp + 1)
    manager.monitor(now=NOW + M15, latest_bid=trade.tp + 1)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE trades SET pnl_usd = 999999 WHERE id = ?", (trade.id,))
    # The review column is the one permitted exception.
    trades_db.attach_review(conn, trade.id, "reviewed by the daily job")
    reloaded = trades_db.get(conn, trade.id)
    assert reloaded is not None and reloaded.review is not None


# ------------------------------------------------------------------ reconcile


def test_boot_adopts_a_position_it_has_a_record_of(
    manager: PositionManager,
    broker: FakeBroker,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    trade = _open(manager, champion_id, config)
    report = manager.reconcile_on_boot(now=NOW + M15)
    assert report.adopted == [trade.id]
    assert report.orphans_flattened == 0


def test_boot_re_protects_an_adopted_position_whose_stop_is_gone(
    manager: PositionManager,
    broker: FakeBroker,
    conn: sqlite3.Connection,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    trade = _open(manager, champion_id, config)
    broker.cancel_order(trade.sl_order_id or "")

    report = manager.reconcile_on_boot(now=NOW + M15)

    assert report.stops_replaced == [trade.id]
    reloaded = trades_db.get(conn, trade.id)
    assert reloaded is not None and reloaded.status is TradeStatus.OPEN_PROTECTED


def test_boot_flattens_a_position_it_has_no_record_of(
    manager: PositionManager, broker: FakeBroker, conn: sqlite3.Connection
) -> None:
    """An orphan cannot be managed, so it is closed rather than adopted."""
    broker.adopt_position(0.25, 3_900.0)

    report = manager.reconcile_on_boot(now=NOW)

    assert report.orphans_flattened == 1
    assert broker.list_positions() == []
    rows = conn.execute(
        "SELECT reason_codes FROM journal_entries WHERE kind = 'risk_event'"
    ).fetchall()
    assert any("ORPHAN_POSITION" in r["reason_codes"] for r in rows)


def test_boot_closes_a_db_row_whose_position_is_gone(
    manager: PositionManager,
    broker: FakeBroker,
    conn: sqlite3.Connection,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    trade = _open(manager, champion_id, config)
    stop = trade.sl
    # The stop filled while the process was down.
    broker.advance(make_bar(NOW + M15, ENTRY, ENTRY, stop - 5, stop - 2))

    report = manager.reconcile_on_boot(now=NOW + 2 * M15)

    assert trade.id in report.closed_from_broker
    reloaded = trades_db.get(conn, trade.id)
    assert reloaded is not None and reloaded.status is TradeStatus.CLOSED


def test_boot_cancels_stray_orders(
    manager: PositionManager, broker: FakeBroker
) -> None:
    broker.submit_order(
        OrderRequest(
            symbol="XAU_USD",
            qty=0.01,
            side=OrderSide.SELL,
            type=BrokerOrderType.STOP_LIMIT,
            time_in_force="gtc",
            client_order_id="stray-1",
            stop_price=3_000.0,
            limit_price=2_990.0,
        )
    )
    report = manager.reconcile_on_boot(now=NOW)
    assert report.stray_orders_cancelled == 1


def test_flatten_all_closes_everything(
    manager: PositionManager, broker: FakeBroker, champion_id: int, config: StrategyConfig
) -> None:
    _open(manager, champion_id, config)
    closed = manager.flatten_all()
    assert len(closed) == 1
    assert closed[0].exit_reason is ExitReason.MANUAL
    assert broker.list_positions() == []


def test_shorts_are_refused_at_the_executor_too(
    manager: PositionManager, champion_id: int, config: StrategyConfig
) -> None:
    order = make_order(version_id=champion_id).model_copy(update={"side": Side.SHORT})
    with pytest.raises(ValueError, match="only long entries"):
        manager.open(order, config=config, decision_close=ENTRY)


def test_the_same_bar_cannot_open_two_positions(
    manager: PositionManager,
    broker: FakeBroker,
    conn: sqlite3.Connection,
    champion_id: int,
    config: StrategyConfig,
) -> None:
    """The entry tag is the idempotency key; a repeat must not insert twice."""
    order = make_order(version_id=champion_id)
    manager.open(order, config=config, decision_close=ENTRY)
    with pytest.raises(sqlite3.IntegrityError):
        manager.open(order, config=config, decision_close=ENTRY)

