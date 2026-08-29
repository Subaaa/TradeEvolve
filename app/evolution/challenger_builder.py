"""ChallengerBuilder: apply a candidate patch to a copy of the champion config.

This is the **only** code path that may write a new `strategy_versions` row.
The LLM produces the candidate as a `HypothesisCandidate`; this module
applies it to the champion's config and produces a new `StrategyConfig`
to be written by the journal.

Schema-validated: any field not in `MUTABLE_BY_LLM` is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.strategy.configs import MUTABLE_BY_LLM, StrategyConfig


class ChallengerError(ValueError):
    pass


@dataclass(frozen=True)
class HypothesisCandidate:
    field: str
    from_value: Any
    to_value: Any
    reason: str
    proposed_at: datetime
    weekly_review_id: int


def apply_candidate(champion: StrategyConfig, candidate: HypothesisCandidate) -> StrategyConfig:
    """Return a new StrategyConfig with the candidate field applied.

    Raises ChallengerError if the field is not in the LLM-mutable allow-list.
    """
    if candidate.field not in MUTABLE_BY_LLM:
        raise ChallengerError(
            f"field '{candidate.field}' is not in the LLM-mutable allow-list"
        )
    current = getattr(champion, candidate.field, None)
    if current != candidate.from_value:
        raise ChallengerError(
            f"champion's current value for '{candidate.field}' ({current!r}) "
            f"does not match candidate's from_value ({candidate.from_value!r})"
        )
    new = champion.model_copy(update={candidate.field: candidate.to_value})
    # Pydantic re-validates invariants on copy
    return new
