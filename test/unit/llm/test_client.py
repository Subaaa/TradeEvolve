"""The LLM client's three responsibilities: validate, retry once, account."""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from pydantic import BaseModel, ConfigDict

from app.db import llm_calls
from app.llm.anthropic_provider import strict_schema
from app.llm.client import LlmClient
from app.llm.fake_provider import FakeProvider
from app.llm.provider import LlmBudgetExceededError, LlmError, LlmInvalidOutputError
from app.llm.schemas import (
    LLM_SITE_BUDGETS,
    ReviewTradeIn,
    ReviewTradeOut,
    WeeklyReviewOut,
)

pytestmark = pytest.mark.unit


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviews: list[str]
    day_summary: str


def payload() -> ReviewTradeIn:
    return ReviewTradeIn(
        day=date(2026, 3, 3),
        strategy_version_tag="t",
        strategy_params={"tp_r_multiple": 2.0},
        trades=[],
    )


def test_a_valid_response_is_parsed(conn: sqlite3.Connection) -> None:
    provider = FakeProvider(
        responses=[{"reviews": ["ok"], "day_summary": "a quiet day"}]
    )
    client = LlmClient(provider, conn)
    out = client.call(site="review_trade", payload=payload(), output_model=Out)
    assert out.day_summary == "a quiet day"


def test_an_invalid_response_gets_exactly_one_corrective_retry(
    conn: sqlite3.Connection,
) -> None:
    provider = FakeProvider(
        responses=[
            {"reviews": "not a list"},
            {"reviews": ["fixed"], "day_summary": "second time"},
        ]
    )
    client = LlmClient(provider, conn)
    out = client.call(site="review_trade", payload=payload(), output_model=Out)

    assert out.day_summary == "second time"
    assert len(provider.calls) == 2
    # The retry must tell the model what was wrong, or it is just a re-roll.
    assert "validation error" in provider.calls[1]["user"].lower()


def test_two_invalid_responses_raise(conn: sqlite3.Connection) -> None:
    provider = FakeProvider(responses=[{"wrong": 1}, {"still": "wrong"}])
    client = LlmClient(provider, conn)
    with pytest.raises(LlmInvalidOutputError):
        client.call(site="review_trade", payload=payload(), output_model=Out)
    assert len(provider.calls) == 2


def test_a_transport_failure_is_not_retried(conn: sqlite3.Connection) -> None:
    provider = FakeProvider(fail_with=LlmError("connection reset"))
    client = LlmClient(provider, conn)
    with pytest.raises(LlmError, match="connection reset"):
        client.call(site="review_trade", payload=payload(), output_model=Out)
    assert len(provider.calls) == 1


def test_every_attempt_is_accounted_for(conn: sqlite3.Connection) -> None:
    provider = FakeProvider(
        responses=[{"bad": True}, {"reviews": [], "day_summary": "ok"}]
    )
    LlmClient(provider, conn).call(
        site="review_trade", payload=payload(), output_model=Out
    )
    rows = llm_calls.recent(conn)
    assert len(rows) == 2
    assert [bool(r["ok"]) for r in rows] == [True, False]  # newest first
    assert llm_calls.tokens_today(conn) > 0


def test_the_budget_is_read_from_the_database_not_memory(
    conn: sqlite3.Connection,
) -> None:
    """A restart must not reset the day's spend."""
    _, max_out = LLM_SITE_BUDGETS["review_trade"]
    llm_calls.insert(
        conn,
        site="review_trade",
        model="m",
        usage_input=9_000,
        usage_output=1_000,
        cache_read=0,
        latency_ms=1,
        ok=True,
        error=None,
        request_id=None,
    )
    # A brand-new client, as if the process had just restarted.
    client = LlmClient(FakeProvider(), conn, daily_token_cap=10_000 + max_out - 1)
    assert client.tokens_used_today() == 10_000
    with pytest.raises(LlmBudgetExceededError):
        client.call(site="review_trade", payload=payload(), output_model=Out)


def test_call_with_default_swallows_failures(conn: sqlite3.Connection) -> None:
    provider = FakeProvider(fail_with=LlmError("down"))
    fallback = Out(reviews=[], day_summary="unavailable")
    out = LlmClient(provider, conn).call_with_default(
        site="review_trade", payload=payload(), output_model=Out, default=fallback
    )
    assert out is fallback


def test_the_fake_provider_can_satisfy_the_real_schemas(
    conn: sqlite3.Connection,
) -> None:
    """`cli evolve --dry-run` depends on this working with no API key."""
    client = LlmClient(FakeProvider(), conn)
    out = client.call(
        site="weekly_review", payload=payload(), output_model=WeeklyReviewOut
    )
    assert isinstance(out, WeeklyReviewOut)


# ------------------------------------------------------------- schema shaping


def test_strict_schema_requires_every_property() -> None:
    """Structured outputs reject a schema with optional properties."""
    schema = strict_schema(ReviewTradeOut.model_json_schema())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_strict_schema_recurses_into_definitions() -> None:
    schema = strict_schema(ReviewTradeOut.model_json_schema())
    for definition in schema.get("$defs", {}).values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
            assert set(definition["required"]) == set(definition["properties"])


def test_the_weekly_candidate_field_is_constrained_to_the_allow_list() -> None:
    from app.strategy.configs import MUTABLE_BY_LLM

    schema = WeeklyReviewOut.model_json_schema()
    hypothesis = schema["$defs"]["HypothesisOut"]
    allowed = set(hypothesis["properties"]["field"]["enum"])
    assert allowed == set(MUTABLE_BY_LLM)
