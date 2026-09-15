"""LlmClient: validation, one corrective retry, and honest accounting.

Three things this owns that a provider must not:

*   **Validation.** The provider returns a dict; this turns it into a typed
    model or fails. On a validation failure it retries exactly once, appending
    the validation error to the user message so the model can see what it got
    wrong. A second failure is a failure.
*   **Budget.** The daily cap is computed from the `llm_calls` table, not from
    a counter in memory. The old implementation debited an optimistic estimate
    into a process-local variable, so a restart reset the day's spend to zero
    and the actual token usage was never read at all.
*   **Accounting.** Every attempt, successful or not, writes a row with the
    real usage from the response.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.db import llm_calls

from .prompts.registry import system_prompt
from .provider import (
    JsonResult,
    LlmBudgetExceededError,
    LlmError,
    LlmInvalidOutputError,
    Provider,
    Usage,
)
from .schemas import LLM_SITE_BUDGETS, SITE_EFFORT, Site

logger = logging.getLogger(__name__)

TOut = TypeVar("TOut", bound=BaseModel)


class LlmClient:
    def __init__(
        self,
        provider: Provider,
        conn: sqlite3.Connection | None = None,
        *,
        daily_token_cap: int = 400_000,
    ) -> None:
        self._provider = provider
        self._conn = conn
        self._cap = daily_token_cap

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def tokens_used_today(self) -> int:
        if self._conn is None:
            return 0
        return llm_calls.tokens_today(self._conn)

    def would_exceed_budget(self, site: Site) -> bool:
        _, max_out = LLM_SITE_BUDGETS[site]
        return self.tokens_used_today() + max_out > self._cap

    def call(
        self,
        *,
        site: Site,
        payload: BaseModel,
        output_model: type[TOut],
        extra_instruction: str = "",
    ) -> TOut:
        """Run one call site and return a validated output model."""
        if self.would_exceed_budget(site):
            raise LlmBudgetExceededError(
                f"daily token cap of {self._cap} would be exceeded by a '{site}' call"
            )

        _, max_out = LLM_SITE_BUDGETS[site]
        system = system_prompt(site)
        user = payload.model_dump_json(indent=2)
        if extra_instruction:
            user = f"{user}\n\n{extra_instruction}"

        schema = output_model.model_json_schema()
        last_error: Exception | None = None

        for attempt in range(2):
            started = time.monotonic()
            try:
                result = self._provider.complete_json(
                    system=system,
                    user=user,
                    schema=schema,
                    max_output_tokens=max_out,
                    effort=SITE_EFFORT[site],
                )
            except LlmError as exc:
                self._record(site, None, started, ok=False, error=str(exc))
                raise

            try:
                parsed = output_model.model_validate(result.data)
            except ValidationError as exc:
                last_error = exc
                self._record(site, result, started, ok=False, error=str(exc)[:500])
                if attempt == 0:
                    logger.warning("LLM output failed validation; retrying once: %s", exc)
                    user = (
                        f"{user}\n\nYour previous response did not satisfy the schema. "
                        f"The validation error was:\n{exc}\n"
                        "Return a corrected response."
                    )
                    continue
                raise LlmInvalidOutputError(
                    f"'{site}' output failed validation twice: {exc}"
                ) from exc

            self._record(site, result, started, ok=True, error=None)
            return parsed

        raise LlmInvalidOutputError(f"'{site}' produced no valid output: {last_error}")

    def call_with_default(
        self,
        *,
        site: Site,
        payload: BaseModel,
        output_model: type[TOut],
        default: TOut,
    ) -> TOut:
        """Call the site, falling back to `default` on any failure.

        Used by jobs that must not take the scheduler down: a missed weekly
        review is a missed week, not an outage.
        """
        try:
            return self.call(site=site, payload=payload, output_model=output_model)
        except LlmError as exc:
            logger.error("LLM call '%s' failed; using default: %s", site, exc)
            return default

    def _record(
        self,
        site: str,
        result: JsonResult | None,
        started: float,
        *,
        ok: bool,
        error: str | None,
    ) -> None:
        if self._conn is None:
            return
        usage = result.usage if result else Usage()
        llm_calls.insert(
            self._conn,
            site=site,
            model=result.model if result else getattr(self._provider, "model", "unknown"),
            usage_input=usage.input_tokens,
            usage_output=usage.output_tokens,
            cache_read=usage.cache_read_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
            ok=ok,
            error=error,
            request_id=result.request_id if result else None,
        )


def build_provider(settings: object) -> Provider:
    """Construct the provider named by `LLM_PROVIDER`."""
    name = getattr(settings, "llm_provider", "anthropic")
    if name == "fake":
        from .fake_provider import FakeProvider

        return FakeProvider()
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            api_key=str(getattr(settings, "anthropic_api_key", "")),
            model=str(getattr(settings, "llm_model", "claude-opus-5")),
            timeout=float(getattr(settings, "llm_timeout_sec", 120.0)),
            max_retries=int(getattr(settings, "llm_max_retries", 3)),
        )
    raise LlmError(f"unknown LLM provider {name!r}")


def build_client(settings: object, conn: sqlite3.Connection | None) -> LlmClient:
    return LlmClient(
        build_provider(settings),
        conn,
        daily_token_cap=int(getattr(settings, "llm_daily_hard_token_cap", 400_000)),
    )
