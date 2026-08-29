"""News clustering: greedy Jaccard-based grouping of related items.

We don't try to be clever here. The point of clustering is to avoid
asking the LLM to classify the same story five times when five
outlets syndicate it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _Tokens:
    text: str
    tokens: frozenset[str]


def _tokenize(text: str) -> frozenset[str]:
    return frozenset(t for t in text.lower().split() if len(t) >= 4)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster(items: list, *, threshold: float = 0.6) -> list[list]:
    """Return a list of clusters; each cluster is a list of items.

    Items must have a `.title` and `.body`. The first item in each cluster
    is the "representative" we use to ask the LLM.
    """
    if not items:
        return []
    tokenized = [_Tokens(text=f"{i.title} {i.body}", tokens=_tokenize(f"{i.title} {i.body}")) for i in items]
    clusters: list[list[int]] = []
    for i, t in enumerate(tokenized):
        placed = False
        for cluster_idx in clusters:
            rep_idx = cluster_idx[0]
            if _jaccard(t.tokens, tokenized[rep_idx].tokens) >= threshold:
                cluster_idx.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
    return [[items[i] for i in c] for c in clusters]
