"""Prompt registry. One system prompt per LLM call site."""
from .registry import PROMPTS, system_prompt

__all__ = ["PROMPTS", "system_prompt"]
