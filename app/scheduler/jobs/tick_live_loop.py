"""Live tick: every minute during market hours.

The only job that calls the StrategyRunner and the RiskEngine. Reads
candles already written by `market_data_backfill` (it does not talk to
the broker for market data itself), builds a `MarketState`, gets a
`Proposal` from the deterministic `StrategyRunner`, runs it through the
`RiskEngine`, journals every outcome, and submits approved orders via
`OrderGateway`.

Scope for this pass: entries only -- no SL/TP/time-stop exit management
yet (Alpaca has no native bracket orders for crypto; see
`services/alpaca_mcp/`). `RiskLimits.max_open_positions=1` means the
risk engine simply won't propose a second position while one is open.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from app.config import get_settings
from app.db import strategy_versions, system_state_store
from app.journal.types import JournalKind, JournalState
from app.market import state_builder
from app.market.indicators.atr import atr
from app.orders.gateway import OrderGateway
from app.orders.mcp_client import McpClientPool
from app.risk import engine as risk_engine
from app.risk.types import AccountState, MarketState
from app.scheduler import session
from app.scheduler.deps import get_admin_engine, get_candle_store, get_journal_writer
from app.strategy import runner as strategy_runner
from app.strategy.configs import StrategyConfig
from pinned.news_policy import NewsPolicy
from pinned.risk_limits import RISK_LIMITS

logger = logging.getLogger(__name__)

_BASELINE_STRATEGY_PATH = (
    Path(__file__).resolve().parents[3] / "pinned" / "strategies" / "ema_atr_xauusd_h1_v1.json"
)

_DECISION_STATE = {
    "approved": JournalState.APPROVED,
    "rejected": JournalState.RISK_REJECTED,
    "skipped": JournalState.RISK_SKIPPED,
}


async def _tick() -> None:
    settings = get_settings()
    candles = get_candle_store().read(settings.instrument, settings.granularity, limit=300)
    if candles.empty:
        logger.debug("tick_live_loop: no candles yet")
        return

    atr_series = atr(candles["h"], candles["l"], candles["c"], 14)
    indicators = {}
    if not atr_series.empty and not pd.isna(atr_series.iloc[-1]):
        indicators["atr_14"] = float(atr_series.iloc[-1])

    market_state = state_builder.build(
        instrument=settings.instrument,
        granularity=settings.granularity,
        candles=candles,
        indicators=indicators,
        news_policy=NewsPolicy.NORMAL,
    )

    mcp_pool = McpClientPool()
    try:
        await _decide_and_submit(candles, market_state, mcp_pool)
    finally:
        await mcp_pool.close()


async def _decide_and_submit(
    candles: pd.DataFrame, market_state: MarketState, mcp_pool: McpClientPool
) -> None:
    account_summary = await mcp_pool.call_tool("get_account_summary", {})
    positions = await mcp_pool.call_tool("get_open_positions", {})
    equity = float(account_summary["account"]["equity"])
    open_positions = len(positions.get("positions", []))

    admin_engine = get_admin_engine()
    running = system_state_store.load_running_state(admin_engine)
    kill_switch = system_state_store.load_kill_switch(admin_engine)
    running.equity_high = max(running.equity_high, equity)
    system_state_store.save_running_state(admin_engine, running)

    account_state = AccountState(
        equity=equity,
        equity_high=running.equity_high,
        day_pnl=running.day_pnl,
        week_pnl=running.week_pnl,
        drawdown_pct=(running.equity_high - equity) / running.equity_high
        if running.equity_high > 0
        else 0.0,
        open_positions=open_positions,
        last_n_trade_outcomes=running.last_n_trade_outcomes,
        consecutive_losses=running.consecutive_losses,
        last_trade_close_ts=running.last_trade_close_ts,
        is_kill_switch=kill_switch,
    )

    strategy_config = StrategyConfig.from_json_file(_BASELINE_STRATEGY_PATH)
    strategy_version_id = strategy_versions.get_or_create_champion(admin_engine, strategy_config)

    journal_writer = get_journal_writer()

    proposals = strategy_runner.propose(
        candles,
        pd.Series(market_state.indicators, dtype=float),
        strategy_config,
        equity=equity,
        strategy_version_id=strategy_version_id,
        session_id=str(session.SESSION_ID),
    )
    if not proposals:
        journal_writer.write(
            session_id=session.SESSION_ID,
            kind=JournalKind.TRADE_DECISION,
            strategy_version_id=strategy_version_id,
            payload={"reason": "no_signal"},
            next_state=JournalState.NO_SIGNAL,
        )
        logger.debug("tick_live_loop: no signal")
        return

    proposal = proposals[0]
    now = datetime.now(tz=UTC)
    decision = risk_engine.evaluate(proposal, account_state, RISK_LIMITS, NewsPolicy.NORMAL, market_state, now)

    journal_writer.write(
        session_id=session.SESSION_ID,
        kind=JournalKind.TRADE_DECISION,
        strategy_version_id=strategy_version_id,
        payload={
            "proposal": proposal.model_dump(mode="json"),
            "decision": decision.decision,
            "notes": decision.notes,
        },
        reason_codes=[r.value for r in decision.reason_codes],
        next_state=_DECISION_STATE[decision.decision],
    )

    if decision.decision != "approved" or decision.order is None:
        logger.info(
            "tick_live_loop: proposal %s: %s", decision.decision, [r.value for r in decision.reason_codes]
        )
        return

    order_gateway = OrderGateway(mcp_pool)
    try:
        fill = await order_gateway.submit(decision.order)
    except Exception:
        logger.exception("tick_live_loop: order submit failed")
        journal_writer.write(
            session_id=session.SESSION_ID,
            kind=JournalKind.TRADE_DECISION,
            strategy_version_id=strategy_version_id,
            payload={"client_tag": decision.order.client_tag, "error": "submit_failed"},
            prev_state=JournalState.APPROVED,
            next_state=JournalState.ORDER_REJECTED,
        )
        return

    journal_writer.write(
        session_id=session.SESSION_ID,
        kind=JournalKind.TRADE_DECISION,
        strategy_version_id=strategy_version_id,
        payload={"client_tag": decision.order.client_tag, "fill": fill},
        prev_state=JournalState.APPROVED,
        next_state=JournalState.ORDER_ACK,
    )
    logger.info("tick_live_loop: order submitted client_tag=%s fill=%s", decision.order.client_tag, fill)


def run() -> None:
    """One live tick. Never raises -- a bad tick is logged, not fatal to the scheduler."""
    try:
        asyncio.run(_tick())
    except Exception:
        logger.exception("tick_live_loop: tick failed")
