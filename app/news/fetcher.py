"""News fetcher: Investing.com RSS for XAU/USD, USD, and Most Read Analysis.

The fetcher is intentionally minimal: it parses RSS via `feedparser`,
normalizes each item, computes a dedup hash, and returns a list of
`RawNewsItem`s. The pipeline decides which items to store and which
to ask the LLM to classify.

Investing.com RSS endpoints (as of the plan's authoring date):
- https://www.investing.com/rss/news_25.rss        (forex / XAU/USD-ish)
- https://www.investing.com/rss/news_14.rss        (commodities / metals)
- https://www.investing.com/rss/news_1.rss         (most read)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import feedparser  # type: ignore[import-untyped]
import httpx

from .dedup import dedup_hash


logger = logging.getLogger(__name__)


DEFAULT_FEEDS: tuple[tuple[str, str], ...] = (
    ("https://www.investing.com/rss/news_25.rss", "investing_forex"),
    ("https://www.investing.com/rss/news_14.rss", "investing_commodities"),
    ("https://www.investing.com/rss/news_1.rss", "investing_mostread"),
)


@dataclass(frozen=True)
class RawNewsItem:
    url: str
    source: str
    title: str
    body: str
    published_at: datetime | None
    dedup_hash: str


class FetchError(RuntimeError):
    pass


def fetch_one(url: str, source: str, *, client: httpx.Client | None = None) -> list[RawNewsItem]:
    """Fetch and parse a single RSS feed. Returns [] on transient errors."""
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=15.0, follow_redirects=True)
    try:
        resp = client.get(url, headers={"User-Agent": "TradeEvolve/0.1"})
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("news fetch failed url=%s err=%s", url, e)
        if owns_client:
            client.close()
        return []
    feed = feedparser.parse(resp.text)
    out: list[RawNewsItem] = []
    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        body = (entry.get("summary") or entry.get("description") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        published = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                published = datetime(*entry.published_parsed[:6])
            except (TypeError, ValueError):
                published = None
        out.append(
            RawNewsItem(
                url=link,
                source=source,
                title=title,
                body=body,
                published_at=published,
                dedup_hash=dedup_hash(title, body),
            )
        )
    if owns_client:
        client.close()
    return out


def fetch_all(feeds: Iterable[tuple[str, str]] = DEFAULT_FEEDS) -> list[RawNewsItem]:
    """Fetch and dedup all configured feeds in one pass."""
    seen: set[str] = set()
    out: list[RawNewsItem] = []
    for url, source in feeds:
        for item in fetch_one(url, source):
            if item.dedup_hash in seen:
                continue
            seen.add(item.dedup_hash)
            out.append(item)
    return out
