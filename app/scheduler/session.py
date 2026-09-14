"""One session id for the lifetime of this process.

The journal hash chain and (eventually) order-dedup key off `session_id`
(see `app/journal/writer.py`), so it must stay stable across ticks
within one running process -- not regenerated per tick.
"""
from __future__ import annotations

from uuid import UUID, uuid4

SESSION_ID: UUID = uuid4()
