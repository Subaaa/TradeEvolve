"""Hourly: walk the journal hash chain and shout if a link is broken."""
from __future__ import annotations

import logging

from app.db.journal_store import SqliteJournalStore
from app.journal.hash_chain import verify_chain
from app.journal.payloads import SystemEventPayload
from app.journal.types import JournalKind
from app.scheduler import deps

logger = logging.getLogger(__name__)


def run() -> None:
    try:
        _verify()
    except Exception:
        logger.exception("audit_verify failed")


def _verify() -> None:
    conn = deps.db()
    store = SqliteJournalStore(conn)
    bad: list[tuple[str, str]] = []

    for session_id in store.list_sessions():
        ok, first_bad = verify_chain(store.list_for_session(session_id))
        if not ok and first_bad is not None:
            bad.append((session_id, first_bad))

    if not bad:
        logger.debug("journal hash chain verified for every session")
        return

    logger.error("journal hash chain broken: %s", bad)
    deps.journal().write(
        kind=JournalKind.SYSTEM_EVENT,
        payload=SystemEventPayload(
            event="journal_chain_broken",
            detail="the hash chain does not verify; the journal may have been altered",
            data={"sessions": [{"session_id": s, "first_bad_entry": e} for s, e in bad]},
        ),
    )
