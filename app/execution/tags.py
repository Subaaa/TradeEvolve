"""Client order tags.

The entry tag is a pure function of (strategy version, side, signal bar). That
is what makes it an idempotency key: re-evaluating the same bar produces the
same `client_order_id`, and Alpaca refuses the duplicate itself. Anything
that varies per tick — equity, a session id, a wall clock — would silently
turn the broker's duplicate protection off.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.risk.types import Side

PREFIX = "te"
MAX_LEN = 48


def make_tag(*, strategy_version_id: int, side: Side, bar_ts: datetime) -> str:
    """Deterministic entry tag for one (version, side, bar) triple."""
    moment = bar_ts if bar_ts.tzinfo else bar_ts.replace(tzinfo=UTC)
    src = f"{strategy_version_id}|{side.value}|{moment.astimezone(UTC).isoformat()}"
    digest = hashlib.sha256(src.encode("utf-8")).hexdigest()[:24]
    return f"{PREFIX}-{digest}"


def stop_tag(entry_tag: str, *, attempt: int = 0) -> str:
    """Tag for the protective stop belonging to `entry_tag`.

    `attempt` distinguishes a replacement stop from the original: a broker
    will reject a re-used client id, and re-placing a lost stop is exactly
    the case where we must not be blocked by our own idempotency key.
    """
    suffix = "sl" if attempt == 0 else f"sl{attempt}"
    return f"{entry_tag}-{suffix}"[:MAX_LEN]


def exit_tag(entry_tag: str, *, reason: str, attempt: int = 0) -> str:
    """Tag for a discretionary exit (take-profit, time stop, kill switch...)."""
    short = reason.replace("_", "")[:8]
    suffix = short if attempt == 0 else f"{short}{attempt}"
    return f"{entry_tag}-x-{suffix}"[:MAX_LEN]
