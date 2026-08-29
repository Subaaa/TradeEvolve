"""News policy: how news categories map to risk policy states.

The LLM does not choose this mapping. The LLM may *classify* news; the
classification then maps to one of three policy states via these rules.
"""
from __future__ import annotations

from enum import Enum
from typing import Final


class NewsCategory(str, Enum):
    GEOPOLITICAL = "geopolitical"
    CENTRAL_BANK = "central_bank"
    DATA_RELEASE = "data_release"
    REGULATORY = "regulatory"
    OTHER = "other"


class NewsPolicy(str, Enum):
    NORMAL = "normal"
    REDUCE_SIZE = "reduce_size"
    NO_NEW_POSITIONS = "no_new_positions"


# A high-impact news flag in the next WINDOW_MIN minutes reduces size;
# the closer to the event, the stricter the policy.
WINDOW_MIN: Final[int] = 240   # 4h lookahead

# Categories that trigger a "reduce_size" policy when in the next 2h
REDUCE_SIZE_CATEGORIES: Final[frozenset[NewsCategory]] = frozenset({
    NewsCategory.CENTRAL_BANK,
    NewsCategory.DATA_RELEASE,
})

# Categories that trigger "no_new_positions" when in the next 30 min
NO_NEW_CATEGORIES: Final[frozenset[NewsCategory]] = frozenset({
    NewsCategory.CENTRAL_BANK,
    NewsCategory.DATA_RELEASE,
})

# Categories considered "high-impact" for the LLM-classification gate
HIGH_IMPACT_CATEGORIES: Final[frozenset[NewsCategory]] = frozenset({
    NewsCategory.CENTRAL_BANK,
    NewsCategory.DATA_RELEASE,
    NewsCategory.GEOPOLITICAL,
})

# The size factor when policy == REDUCE_SIZE
REDUCE_SIZE_FACTOR: Final[float] = 0.5

# The LLM-required minimum confidence to act on a classification
MIN_CONFIDENCE: Final[float] = 0.6


def policy_for_flags(
    *,
    has_central_bank_within_2h: bool,
    has_data_release_within_2h: bool,
    has_any_high_impact_within_30m: bool,
) -> NewsPolicy:
    """Pure mapping from event flags to a risk policy.

    The LLM produces the flags via classify_news; this function decides
    what they mean.
    """
    if has_any_high_impact_within_30m:
        return NewsPolicy.NO_NEW_POSITIONS
    if has_central_bank_within_2h or has_data_release_within_2h:
        return NewsPolicy.REDUCE_SIZE
    return NewsPolicy.NORMAL
