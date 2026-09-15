"""The trading tick: fires just after each M15 bar closes.

Runs at :00, :15, :30 and :45 plus a short delay, not every minute. The old
loop ran every 60 seconds against hourly bars, so the same signal was
re-evaluated sixty times and the only thing preventing sixty orders was a
position check against a possibly-stale broker snapshot.

Two guards make a repeated evaluation harmless anyway:

*   `last_processed_bar_ts` in `system_state`, so a bar is acted on once even
    if the job is triggered twice;
*   the entry tag, which is a pure function of (version, side, bar), so the
    broker refuses a duplicate `client_order_id` on its own.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app.db import candles as candles_db
from app.db import strategy_versions as sv_db
from app.db import system_state as ss
from app.journal.payloads import AccountSnapshot, NoSignalPayload, TradeDecisionPayload
from app.journal.types import JournalKind, JournalState
from app.market.bars import (
    drop_forming_bar,
    expected_last_closed_bar,
    granularity_seconds,
    to_dataframe,
)
from app.risk import kill_switch
from app.risk.account_state import build_from_db
from app.risk.engine import evaluate
from app.risk.types import AccountState, MarketState
from app.scheduler import deps
from app.strategy.configs import StrategyConfig
from app.strategy.runner import propose, required_bars
from pinned.risk_limits import RISK_LIMITS

logger = logging.getLogger(__name__)


def run() -> None:
    try:
        _tick(datetime.now(tz=UTC))
    except Exception:
        # A raising job kills the APScheduler thread; a logged one does not.
        logger.exception("tick_bar_close failed")


def _tick(now: datetime) -> None:
    s = deps.settings()
    conn = deps.db()
    broker = deps.broker()
    journal = deps.journal()

    ss.touch_last_tick(conn, now=now)

    champion = sv_db.ensure_champion(
        conn, StrategyConfig.from_json_file(s.resolved_champion_config())
    )
    config = champion.config

    # Refresh candles straight from the broker. The old design read from the
    # database and relied on a separate 5-minute backfill, so the strategy
    # could act on data up to five minutes plus one bar stale.
    fetched = broker.fetch_bars(
        s.instrument, s.granularity, limit=max(400, required_bars(config) + 50)
    )
    if fetched:
        candles_db.upsert_bars(conn, s.instrument, s.granularity, fetched)

    closed = drop_forming_bar(to_dataframe(fetched), s.granularity, now)
    if closed.empty:
        logger.warning("no closed bars available; skipping tick")
        return

    last_bar_ts = closed.index[-1].to_pydatetime()
    expected = expected_last_closed_bar(now, s.granularity)
    if last_bar_ts < expected:
        logger.warning(
            "latest closed bar is %s but %s was expected; data is behind",
            last_bar_ts.isoformat(),
            expected.isoformat(),
        )

    already = ss.load_last_processed_bar_ts(conn)
    if already is not None and already >= last_bar_ts:
        logger.debug("bar %s already processed", last_bar_ts.isoformat())
        return

    account = broker.get_account()
    positions = [p for p in broker.list_positions() if p.qty > 0]

    state = build_from_db(
        conn,
        equity=account.equity,
        open_positions=len(positions),
        now=now,
        limits=RISK_LIMITS,
        granularity=s.granularity,
    )

    # Engage the kill switch before deciding, not after: a breach discovered
    # on this tick must block this tick.
    decision_to_kill = kill_switch.check_auto_kill(state, RISK_LIMITS)
    if decision_to_kill is not None and not state.is_kill_switch:
        kill_switch.engage(
            conn,
            reason=decision_to_kill.reason,
            requires_manual_clear=decision_to_kill.requires_manual_clear,
            now=now,
        )
        state = state.model_copy(
            update={"is_kill_switch": True, "kill_switch_reason": decision_to_kill.reason}
        )

    ss.save_last_processed_bar_ts(conn, last_bar_ts)

    proposals = propose(
        closed,
        config,
        equity=account.equity,
        strategy_version_id=champion.id,
    )
    if not proposals:
        journal.write(
            kind=JournalKind.TRADE_DECISION,
            payload=NoSignalPayload(bar_ts=last_bar_ts),
            strategy_version_id=champion.id,
            next_state=JournalState.NO_SIGNAL,
        )
        return

    proposal = proposals[0]
    # Staleness is measured from when the bar CLOSED, not when it opened. A
    # bar stamped `t` covers `[t, t + period)`, so measuring from `t` would
    # make every M15 bar 900 seconds old the instant it became usable and
    # trip the stale-data gate on every single tick.
    bar_closed_at = last_bar_ts + timedelta(seconds=granularity_seconds(s.granularity))
    seconds_old = max(0, int((now - bar_closed_at).total_seconds()))
    market = MarketState(
        instrument=proposal.instrument,
        granularity=s.granularity,
        last_bar_ts=last_bar_ts,
        last_close=float(closed["c"].iloc[-1]),
        indicators=proposal.indicators,
        seconds_since_last_bar=seconds_old,
    )

    decision = evaluate(proposal, state, RISK_LIMITS, market, now)

    journal.write(
        kind=JournalKind.TRADE_DECISION,
        payload=TradeDecisionPayload(
            bar_ts=last_bar_ts,
            decision=decision.decision,
            reason_codes=decision.reason_codes,
            client_tag=proposal.client_tag,
            side=proposal.side,
            units=proposal.units,
            entry_price=proposal.entry_price,
            stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit,
            expected_rr=proposal.expected_rr,
            expected_risk_usd=proposal.expected_risk_usd,
            indicators=proposal.indicators,
            account=_snapshot(state),
            notes=decision.notes,
        ),
        strategy_version_id=champion.id,
        prev_state=JournalState.DRAFT,
        next_state=_state_for(decision.decision),
        reason_codes=[c.value for c in decision.reason_codes],
    )

    if not decision.approved or decision.order is None:
        logger.info(
            "signal on %s not taken: %s (%s)",
            last_bar_ts.isoformat(),
            decision.decision,
            ", ".join(c.value for c in decision.reason_codes),
        )
        return

    deps.position_manager().open(
        decision.order,
        config=config,
        decision_close=float(closed["c"].iloc[-1]),
        now=now,
    )


def _state_for(decision: str) -> JournalState:
    match decision:
        case "approved":
            return JournalState.APPROVED
        case "skipped":
            return JournalState.RISK_SKIPPED
        case _:
            return JournalState.RISK_REJECTED


def _snapshot(state: AccountState) -> AccountSnapshot:
    return AccountSnapshot(
        equity=state.equity,
        day_start_equity=state.day_start_equity,
        day_pnl=state.day_pnl,
        week_pnl=state.week_pnl,
        drawdown_pct=state.drawdown_pct,
        trades_today=state.trades_today,
        losses_today=state.losses_today,
        consecutive_losses=state.consecutive_losses,
        open_positions=state.open_positions,
        bars_since_last_entry=state.bars_since_last_entry,
        bars_since_last_exit=state.bars_since_last_exit,
        last_exit_reason=state.last_exit_reason,
    )
