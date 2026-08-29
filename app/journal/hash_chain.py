"""Linked-list hash chain for the journal.

Each row's hash is sha256(ts || kind || payload_canonical || prev_hash).
A daily audit job walks the chain and writes a `system_event` if any
link is broken.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .types import JournalEntry


def canonical_payload(payload: dict[str, Any]) -> str:
    """Return a stable canonical JSON for the payload."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(
    *,
    ts_iso: str,
    kind: str,
    payload: dict[str, Any],
    prev_hash: bytes | None,
) -> bytes:
    h = hashlib.sha256()
    h.update(ts_iso.encode("utf-8"))
    h.update(b"|")
    h.update(kind.encode("utf-8"))
    h.update(b"|")
    h.update(canonical_payload(payload).encode("utf-8"))
    h.update(b"|")
    if prev_hash:
        h.update(prev_hash)
    return h.digest()


def verify_chain(entries: list[JournalEntry]) -> tuple[bool, str | None]:
    """Walk the chain. Return (ok, first_bad_entry_id)."""
    prev: bytes | None = None
    for e in entries:
        ts_iso = e.ts.isoformat()
        expected = compute_hash(
            ts_iso=ts_iso,
            kind=e.kind.value if hasattr(e.kind, "value") else str(e.kind),
            payload=e.payload,
            prev_hash=prev,
        )
        if e.hash is None or e.hash != expected:
            return False, str(e.id) if e.id is not None else "<unsaved>"
        prev = e.hash
    return True, None
