"""Prompt registry. One module per LLM call site."""
from .registry import PROMPTS, Prompt  # re-export

__all__ = ["PROMPTS", "Prompt"]
