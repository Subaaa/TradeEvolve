"""The provider boundary.

`LlmClient` owns validation, retries and accounting; a provider owns nothing
but the wire format. Keeping the two apart is what made replacing the previous
vendor a single new file rather than a rewrite, and it is why adding a second
provider later would be another single file.

A provider never sees the database, the broker, or any credential other than
its own API key.
"""
from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

Effort = Literal["low", "medium", "high"]


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class JsonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any]
    usage: Usage
    model: str
    request_id: str | None = None


class LlmError(RuntimeError):
    """Transport, budget or protocol failure. Never raised for bad content."""


class LlmInvalidOutputError(LlmError):
    """The model returned JSON that does not satisfy the output schema."""


class LlmBudgetExceededError(LlmError):
    """The daily token cap would be exceeded by this call."""


class Provider(Protocol):
    name: str

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_output_tokens: int,
        effort: Effort = "medium",
    ) -> JsonResult: ...
