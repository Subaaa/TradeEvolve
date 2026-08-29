"""News pipeline: fetch -> dedup -> cluster -> classify (LLM) -> persist flags."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from pinned.news_policy import NewsCategory, NewsPolicy

from .cluster import cluster
from .fetcher import RawNewsItem, fetch_all
from .dedup import dedup_hash


logger = logging.getLogger(__name__)


class NewsStore(Protocol):
    def seen(self, dedup_hash: str) -> bool: ...
    def record(
        self,
        *,
        url: str,
        source: str,
        title: str,
        body: str,
        published_at: datetime | None,
        dedup_hash: str,
        cluster_id: int | None,
    ) -> int: ...
    def store_classification(
        self,
        *,
        news_id: int,
        category: NewsCategory,
        asset_tags: list[str],
        sentiment: float,
        confidence: float,
        is_fact: bool,
        summary: str,
    ) -> None: ...


class LlmClassifierLike(Protocol):
    def classify(self, item: RawNewsItem) -> "ClassifiedItem": ...  # type: ignore[name-defined]


@dataclass(frozen=True)
class ClassifiedItem:
    category: NewsCategory
    asset_tags: list[str]
    sentiment: float
    confidence: float
    is_fact: bool
    summary: str


class StubLlmClassifier:
    """Used in tests; never returns is_fact=True so the policy stays 'normal'."""

    def classify(self, item: RawNewsItem) -> ClassifiedItem:
        return ClassifiedItem(
            category=NewsCategory.OTHER,
            asset_tags=[],
            sentiment=0.0,
            confidence=0.0,
            is_fact=False,
            summary=item.title[:80],
        )


class NewsPipeline:
    def __init__(self, store: NewsStore, classifier: LlmClassifierLike) -> None:
        self._store = store
        self._classifier = classifier

    def run_once(self, *, now: datetime | None = None) -> int:
        """One pass: fetch, dedup, cluster, classify, store. Returns the number of new items."""
        now = now or datetime.now(tz=timezone.utc)
        items = fetch_all()
        if not items:
            return 0
        # Drop items we've already seen
        new_items = [i for i in items if not self._store.seen(i.dedup_hash)]
        if not new_items:
            return 0
        clusters = cluster(new_items)
        n = 0
        for cluster_items in clusters:
            rep = cluster_items[0]
            classified = self._classifier.classify(rep)
            # Persist the representative and link the others
            news_id = self._store.record(
                url=rep.url,
                source=rep.source,
                title=rep.title,
                body=rep.body,
                published_at=rep.published_at,
                dedup_hash=rep.dedup_hash,
                cluster_id=None,
            )
            self._store.store_classification(
                news_id=news_id,
                category=classified.category,
                asset_tags=classified.asset_tags,
                sentiment=classified.sentiment,
                confidence=classified.confidence,
                is_fact=classified.is_fact,
                summary=classified.summary,
            )
            n += 1
        return n

    def current_policy(
        self,
        *,
        now: datetime | None = None,
        lookahead_min: int = 240,
        next_2h: bool = False,
    ) -> NewsPolicy:
        """Decide the current news policy from the persisted flags.

        For MVP we return NewsPolicy.NORMAL; in Phase 5 this becomes a
        SQL query that joins the classifications table to a calendar.
        """
        # TODO(phase-5): real policy derivation from stored classifications
        return NewsPolicy.NORMAL
