"""Thin Anthropic client wrapper for JSON-only model calls.

Single entry point: ``call_model_json(prompt) -> dict``.

- Reads ``ANTHROPIC_API_KEY`` from the environment.
- Sends the prompt as a user message with a system instruction that enforces
  JSON-only output.
- Parses the response as JSON (tolerant of a single fenced ```json block).
- Raises a distinct exception for each failure mode so callers can act on
  the root cause instead of swallowing a generic error.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any


class ModelClientError(Exception):
    """Base error for all failures in this module."""


class ApiKeyMissingError(ModelClientError):
    """Raised when ANTHROPIC_API_KEY is not set in the environment."""


class ModelCallError(ModelClientError):
    """Raised when the Anthropic API call itself fails."""


class ModelResponseError(ModelClientError):
    """Raised when the response cannot be parsed into a JSON object."""


DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_MAX_TOKENS = 2048
_JSON_SYSTEM_PROMPT = (
    "You are a structured-output engine. Respond with a single valid JSON "
    "object and nothing else. Never wrap the JSON in markdown fences. Never "
    "add commentary, preamble, or trailing text."
)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text.strip()


def _extract_text(response: Any) -> str:
    """Concatenate all text blocks from an Anthropic Messages response."""
    blocks = getattr(response, "content", None) or []
    parts: list[str] = []
    for b in blocks:
        t = getattr(b, "text", None)
        if isinstance(t, str):
            parts.append(t)
    if not parts:
        raise ModelResponseError(
            f"Model response had no text content. Raw response: {response!r}"
        )
    return "".join(parts)


def call_model_json(
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.0,
) -> dict:
    """Call Claude with ``prompt`` and return the parsed JSON object.

    Raises:
        ApiKeyMissingError: ANTHROPIC_API_KEY is not set.
        ModelCallError: The Anthropic SDK raised, or returned a non-success.
        ModelResponseError: The response text was not a JSON object.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ApiKeyMissingError(
            "ANTHROPIC_API_KEY is not set in the environment. "
            "Export it before running enrichment (see script help)."
        )

    try:
        import anthropic  # imported lazily so the module imports cleanly without the dep
    except ImportError as e:
        raise ModelClientError(
            "The 'anthropic' package is not installed. "
            "Run: pip install -r requirements.txt"
        ) from e

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            system=_JSON_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # SDK raises several distinct types; surface them uniformly
        raise ModelCallError(f"Anthropic API call failed: {type(e).__name__}: {e}") from e

    raw_text = _extract_text(response)
    payload = _strip_fence(raw_text)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ModelResponseError(
            f"Response was not valid JSON: {e.msg} at pos {e.pos}. "
            f"Raw text (first 500 chars): {raw_text[:500]!r}"
        ) from e

    if not isinstance(parsed, dict):
        raise ModelResponseError(
            f"Response parsed to {type(parsed).__name__}, expected a JSON object. "
            f"Raw text (first 500 chars): {raw_text[:500]!r}"
        )
    return parsed
