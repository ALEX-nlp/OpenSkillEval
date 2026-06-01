"""ClaudeCodeDS — thin subclass of harbor's ClaudeCode for DeepSeek models.

Only difference from upstream:
  - Agent name (``claude-code-ds``).
  - ``_build_metrics`` tolerates ``None`` values returned by DeepSeek's API.
"""
from __future__ import annotations

from typing import Any

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.models.trajectories import Metrics


class ClaudeCodeDS(ClaudeCode):

    @staticmethod
    def name() -> str:
        return "claude-code-ds"

    @staticmethod
    def _build_metrics(usage: Any) -> Metrics | None:
        if not isinstance(usage, dict):
            return None

        cached_tokens = usage.get("cache_read_input_tokens") or 0
        creation = usage.get("cache_creation_input_tokens") or 0
        input_tokens = usage.get("input_tokens") or 0
        prompt_tokens = input_tokens + cached_tokens + creation
        completion_tokens = usage.get("output_tokens") or 0

        extra: dict[str, Any] = {}
        for key, value in usage.items():
            if key in {"input_tokens", "output_tokens"}:
                continue
            extra[key] = value

        return Metrics(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            extra=extra or None,
        )
