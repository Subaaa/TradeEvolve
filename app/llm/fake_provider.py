"""A deterministic provider for tests and dry runs.

Lets `cli evolve --dry-run` exercise the entire weekly pipeline — stats,
candidate validation, challenger construction, walk-forward, bootstrap — with
no API key and no cost. The canned outputs are minimal but schema-valid, so a
failure in a dry run is a failure in the pipeline, not in the model.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .provider import Effort, JsonResult, LlmError, Usage


def _default_for(schema: dict[str, Any], key_path: str = "") -> Any:
    """Build a minimal value satisfying `schema`."""
    if "anyOf" in schema:
        branches = [b for b in schema["anyOf"] if b.get("type") != "null"]
        if not branches:
            return None
        return _default_for(branches[0], key_path)

    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]

    kind = schema.get("type")
    if kind == "object":
        return {
            name: _default_for(sub, name)
            for name, sub in (schema.get("properties") or {}).items()
        }
    if kind == "array":
        return []
    if kind == "string":
        return f"fake:{key_path}" if key_path else "fake"
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return True
    return None


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(
        self,
        *,
        responses: list[dict[str, Any]] | None = None,
        handler: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._handler = handler
        self._fail_with = fail_with
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_output_tokens: int,
        effort: Effort = "medium",
    ) -> JsonResult:
        self.calls.append(
            {"system": system, "user": user, "schema": schema, "effort": effort}
        )
        if self._fail_with is not None:
            raise self._fail_with

        if self._responses:
            data = self._responses.pop(0)
        elif self._handler is not None:
            data = self._handler(system, user, schema)
        else:
            resolved = _resolve_refs(schema)
            data = _default_for(resolved)
            if not isinstance(data, dict):
                raise LlmError("fake provider could not synthesise an object")

        return JsonResult(
            data=data,
            usage=Usage(
                input_tokens=len(user) // 4,
                output_tokens=len(json.dumps(data)) // 4,
            ),
            model=self.model,
            request_id="fake-request",
        )


def _resolve_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline `$ref`s so the default builder does not have to follow them."""
    defs = schema.get("$defs") or {}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = str(node["$ref"]).rsplit("/", 1)[-1]
                return walk(dict(defs.get(name, {})))
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    resolved = walk(schema)
    return resolved if isinstance(resolved, dict) else {}


def weekly_review_handler(
    champion: Any, field: str = "time_stop_bars"
) -> Callable[[str, str, dict[str, Any]], dict[str, Any]]:
    """A canned weekly review that proposes one genuinely valid change.

    `cli evolve --dry-run` uses this so the dry run exercises the whole
    pipeline — challenger construction, walk-forward, bootstrap — rather than
    stopping at the range check on the synthesised zeros the generic default
    would produce.
    """
    from app.strategy.configs import LLM_RANGES

    low, high = LLM_RANGES[field]
    current = float(getattr(champion, field))
    # Move toward the middle of the allowed range, away from the current value.
    target = high if current < (low + high) / 2 else low

    def handler(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        if "candidate" not in (schema.get("properties") or {}):
            resolved = _resolve_refs(schema)
            built = _default_for(resolved)
            return built if isinstance(built, dict) else {}
        return {
            "summary": "canned dry-run review",
            "failure_patterns": ["time stops dominate the exit mix"],
            "candidate": {
                "field": field,
                "from_value": current,
                "to_value": target,
                "reason": "dry run: exercise the pipeline with a valid change",
            },
            "blocked_reason": None,
        }

    return handler
