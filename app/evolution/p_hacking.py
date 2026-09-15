"""Plateau / p-hacking detection.

After N failed challengers from a given champion, declare a plateau and
ask the LLM to look at *different evidence*. If the LLM ignores the
directive, the next hypothesis is auto-rejected.
"""
from __future__ import annotations

from dataclasses import dataclass

from pinned.promotion_gates import PROMOTION_GATES, PromotionGates


@dataclass(frozen=True)
class PlateauState:
    champion_id: int
    attempts: int
    is_plateau: bool
    next_must_be_different_evidence: bool


def update(
    champion_id: int,
    *,
    current_attempts: int,
    candidate_failed: bool,
    gates: PromotionGates = PROMOTION_GATES,
) -> PlateauState:
    """Update the per-champion attempt counter.

    If the candidate failed, increment. If we just crossed the
    threshold, mark the plateau.
    """
    attempts = current_attempts + 1 if candidate_failed else 0
    is_plateau = attempts >= gates.max_hypothesis_attempts_per_champion
    return PlateauState(
        champion_id=champion_id,
        attempts=attempts,
        is_plateau=is_plateau,
        next_must_be_different_evidence=is_plateau,
    )


def enforce(state: PlateauState, *, has_different_evidence: bool) -> tuple[bool, list[str]]:
    """Return (passes, reason_codes)."""
    if state.is_plateau and not has_different_evidence:
        return False, ["POTENTIAL_P_HACKING"]
    return True, []
