"""News dedup: stable hash on normalized title + first 200 chars of body."""
from __future__ import annotations

import hashlib
import re


_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip."""
    return _WS.sub(" ", text.lower()).strip()


def dedup_hash(title: str, body: str) -> str:
    """Stable dedup key."""
    title_n = normalize(title)
    body_n = normalize(body)[:200]
    h = hashlib.sha256()
    h.update(title_n.encode("utf-8"))
    h.update(b"|")
    h.update(body_n.encode("utf-8"))
    return h.hexdigest()
