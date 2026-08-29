"""Build idempotent OANDA `clientExtensions` tags.

Each approved order carries a `tag` derived from the proposal's
strategy_version_id, market_state_hash, side, and bar_ts. OANDA
rejects re-submission of the same tag, which is our duplicate-order
protection.
"""
from __future__ import annotations

import hashlib


def make_tag(
    *,
    strategy_version_id: int,
    market_state_hash: str,
    side: str,
    bar_ts_iso: str,
) -> str:
    """Return the OANDA `clientExtensions.tag` for a proposal.

    Tags are 1–128 chars, alphanumeric and '-_.' allowed.
    """
    raw = f"v{strategy_version_id}|m{market_state_hash}|{side}|{bar_ts_iso}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"nito-{h}"


def market_state_hash(market_state_dict: dict) -> str:
    """Stable short hash of a market state dict."""
    import json
    canon = json.dumps(market_state_dict, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
