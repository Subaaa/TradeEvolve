"""ChallengerBuilder: apply one bounded candidate change to the champion.

Three independent checks stand between the LLM and the live config, and a
candidate must pass all of them:

1.  **Allow-list.** The field name must be in `MUTABLE_BY_LLM`.
2.  **Range.** The new value must be inside `LLM_RANGES`, which is narrower
    than the pydantic bounds on purpose — a config can be valid without being
    a change the LLM was permitted to propose.
3.  **Optimistic concurrency.** `from_value` must match the champion's
    current value, so a hypothesis written against a config that has since
    been promoted away is rejected rather than silently misapplied.

Pydantic then re-validates every cross-field invariant on the copy, so a
change that would make the slow EMA shorter than the fast one fails here
rather than at the first live tick.
"""
from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, ConfigDict, ValidationError

from app.strategy.configs import (
    INTEGER_FIELDS,
    LLM_RANGES,
    MUTABLE_BY_LLM,
    StrategyConfig,
)


class ChallengerError(ValueError):
    """A candidate that must not be applied. The message is journaled."""


class HypothesisCandidate(BaseModel):
    """One bounded parameter change, as proposed by the weekly review."""

    model_config = ConfigDict(extra="forbid")

    field: str
    from_value: float
    to_value: float
    reason: str = ""
    proposed_at: datetime | None = None
    weekly_review_id: int | None = None


def _coerce(field: str, value: float) -> float | int:
    if field in INTEGER_FIELDS:
        if not math.isclose(value, round(value), abs_tol=1e-9):
            raise ChallengerError(
                f"field '{field}' is an integer parameter; got {value!r}"
            )
        return round(value)
    return float(value)


def validate_candidate(
    champion: StrategyConfig, candidate: HypothesisCandidate
) -> float | int:
    """Check the candidate and return the coerced target value."""
    if candidate.field not in MUTABLE_BY_LLM:
        raise ChallengerError(
            f"field '{candidate.field}' is not in the LLM-mutable allow-list"
        )

    low, high = LLM_RANGES[candidate.field]
    if not (low <= candidate.to_value <= high):
        raise ChallengerError(
            f"to_value {candidate.to_value!r} for '{candidate.field}' is outside "
            f"the allowed range [{low}, {high}]"
        )

    current = getattr(champion, candidate.field)
    if not math.isclose(float(current), float(candidate.from_value), rel_tol=1e-9, abs_tol=1e-9):
        raise ChallengerError(
            f"champion's current value for '{candidate.field}' ({current!r}) does not "
            f"match the candidate's from_value ({candidate.from_value!r})"
        )

    target = _coerce(candidate.field, candidate.to_value)
    if math.isclose(float(target), float(current), rel_tol=1e-9, abs_tol=1e-9):
        raise ChallengerError(
            f"candidate for '{candidate.field}' does not change anything"
        )
    return target


def apply_candidate(
    champion: StrategyConfig,
    candidate: HypothesisCandidate,
    *,
    version_tag: str | None = None,
) -> StrategyConfig:
    """Return a new StrategyConfig with the candidate applied."""
    target = validate_candidate(champion, candidate)
    tag = version_tag or next_version_tag(champion, candidate)
    # Re-validate from a plain dict rather than `model_copy`: a copy skips
    # validation, so an invariant-breaking change (a slow EMA shorter than the
    # fast one) would survive to the first live tick.
    try:
        return StrategyConfig.model_validate(
            champion.model_dump() | {candidate.field: target, "version_tag": tag}
        )
    except ValidationError as exc:
        raise ChallengerError(
            f"applying '{candidate.field}' = {target!r} violates a config invariant: {exc}"
        ) from exc


def next_version_tag(champion: StrategyConfig, candidate: HypothesisCandidate) -> str:
    """A readable, unique-enough tag describing what changed."""
    base = champion.version_tag.split("__", 1)[0]
    value = _coerce(candidate.field, candidate.to_value)
    short = "".join(part[0] for part in candidate.field.split("_"))
    return f"{base}__{short}{value}"
