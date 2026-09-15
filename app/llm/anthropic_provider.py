"""Anthropic (Claude) provider.

Uses the Messages API with structured outputs (`output_config.format`), which
guarantees the response is JSON matching the schema. That is a better fit than
forced tool use here: forced `tool_choice` is rejected on the newest models,
and structured outputs express the same contract everywhere.

Thinking is left unconfigured on purpose. Claude Opus 5 thinks adaptively by
default, and the deprecated `budget_tokens` parameter is rejected outright on
current models. Depth is controlled through `output_config.effort` instead,
which is what the two call sites here vary: a daily trade review is routine
work, a weekly hypothesis is not.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .provider import Effort, JsonResult, LlmError, Provider, Usage

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a pydantic-generated JSON schema acceptable to structured outputs.

    Structured outputs require every object to forbid extra properties and to
    list every property as required. Pydantic omits defaulted fields from
    `required`, so a model with an optional field would be rejected. We add
    them back; optionality is expressed by the field's type allowing null.
    """
    out = dict(schema)

    if "$defs" in out:
        out["$defs"] = {k: strict_schema(v) for k, v in out["$defs"].items()}

    if out.get("type") == "object" or "properties" in out:
        props = out.get("properties")
        if isinstance(props, dict):
            out["properties"] = {k: strict_schema(v) for k, v in props.items()}
            out["required"] = list(out["properties"].keys())
            out["additionalProperties"] = False

    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = strict_schema(items)

    for key in ("anyOf", "oneOf", "allOf"):
        branches = out.get(key)
        if isinstance(branches, list):
            out[key] = [
                strict_schema(b) if isinstance(b, dict) else b for b in branches
            ]

    return out


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise LlmError("ANTHROPIC_API_KEY is not set")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LlmError("the `anthropic` package is not installed") from exc

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_output_tokens: int,
        effort: Effort = "medium",
    ) -> JsonResult:
        anthropic = self._anthropic
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_output_tokens,
                # The system prompt is stable across calls at a site, so it is
                # worth caching; the per-call evidence goes in the user turn.
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user}],
                output_config={
                    "format": {"type": "json_schema", "schema": strict_schema(schema)},
                    "effort": effort,
                },
            )
        except anthropic.RateLimitError as exc:
            raise LlmError(f"rate limited by Anthropic: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LlmError(f"Anthropic returned HTTP {exc.status_code}: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LlmError(f"could not reach Anthropic: {exc}") from exc

        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LlmError(f"model declined the request (category: {category})")
        if stop == "max_tokens":
            raise LlmError(
                f"response hit the {max_output_tokens}-token output cap before finishing"
            )

        text = next(
            (
                str(getattr(block, "text", ""))
                for block in response.content
                if getattr(block, "type", None) == "text"
            ),
            None,
        )
        if not text:
            raise LlmError("Anthropic returned no text block")

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LlmError(f"response was not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise LlmError("response JSON was not an object")

        usage = getattr(response, "usage", None)
        return JsonResult(
            data=data,
            usage=Usage(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            ),
            model=str(getattr(response, "model", self._model)),
            request_id=getattr(response, "_request_id", None),
        )
