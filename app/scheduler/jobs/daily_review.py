"""00:10 UTC: have Claude read yesterday's closed trades.

Skips silently when there is nothing to review. An LLM call on a day with no
trades produces a paragraph about nothing and costs real money.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app.db import strategy_versions as sv_db
from app.db import trades as trades_db
from app.db.journal_store import SqliteJournalStore
from app.db.trades import TradeRecord
from app.journal.payloads import LlmEventPayload
from app.journal.types import JournalKind
from app.llm.client import build_client
from app.llm.provider import LlmError
from app.llm.schemas import ClosedTradeSummary, ReviewTradeIn, ReviewTradeOut
from app.scheduler import deps

logger = logging.getLogger(__name__)


def run() -> None:
    try:
        _review(datetime.now(tz=UTC))
    except Exception:
        logger.exception("daily_review failed")


def _summary(t: TradeRecord) -> ClosedTradeSummary:
    return ClosedTradeSummary(
        trade_id=t.id,
        opened_at=t.opened_at.isoformat(),
        closed_at=t.closed_at.isoformat() if t.closed_at else "",
        side=t.side.value,
        units=t.units,
        entry=t.entry_fill_price or t.decision_close,
        stop_loss=t.sl,
        take_profit=t.tp,
        exit_price=t.exit_price or 0.0,
        exit_reason=t.exit_reason.value if t.exit_reason else "unknown",
        pnl_usd=t.pnl_usd or 0.0,
        pnl_r=t.pnl_r or 0.0,
        fees_usd=t.fees_usd or 0.0,
        mfe_r=t.mfe_r or 0.0,
        mae_r=t.mae_r or 0.0,
        bars_held=t.bars_held or 0,
        indicators=t.indicators,
    )


def _review(now: datetime) -> None:
    conn = deps.db()
    s = deps.settings()
    journal = deps.journal()

    yesterday = (now - timedelta(days=1)).date()
    trades = trades_db.closed_on_day(conn, yesterday)
    if not trades:
        logger.info("no trades closed on %s; skipping the review", yesterday)
        return

    champion = sv_db.load_champion(conn)
    if champion is None:
        logger.warning("no champion configured; skipping the review")
        return

    start = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC)
    context = [
        t for t in trades_db.list_closed(conn, until=start, limit=20)
    ]
    rejections = SqliteJournalStore(conn).count_by_reason_since(start.isoformat())

    payload = ReviewTradeIn(
        day=yesterday,
        strategy_version_tag=champion.version_tag,
        strategy_params={
            k: float(v["value"]) for k, v in champion.config.llm_visible_params().items()
        },
        trades=[_summary(t) for t in trades],
        recent_context=[_summary(t) for t in context],
        day_pnl_usd=sum(float(t.pnl_usd or 0.0) for t in trades),
        rejection_counts=rejections,
    )

    llm = build_client(s, conn)
    try:
        out = llm.call(site="review_trade", payload=payload, output_model=ReviewTradeOut)
    except LlmError as exc:
        logger.error("daily review failed: %s", exc)
        journal.write(
            kind=JournalKind.LLM_EVENT,
            payload=LlmEventPayload(
                site="review_trade", model=s.llm_model, ok=False, error=str(exc)
            ),
            strategy_version_id=champion.id,
        )
        return

    for review in out.reviews:
        text = review.summary
        if review.failure_mode_tags:
            text += "  [" + ", ".join(review.failure_mode_tags) + "]"
        if not review.followed_rules:
            text += "  [RULES NOT FOLLOWED]"
        trades_db.attach_review(conn, review.trade_id, text)

    journal.write(
        kind=JournalKind.LLM_EVENT,
        payload=LlmEventPayload(
            site="review_trade",
            model=s.llm_model,
            ok=True,
            summary=out.day_summary,
        ),
        strategy_version_id=champion.id,
    )
    logger.info("reviewed %d trades from %s", len(out.reviews), yesterday)
