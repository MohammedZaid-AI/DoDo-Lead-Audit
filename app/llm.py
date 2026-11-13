"""Single wrapper for all LLM calls (Groq today; swap the client here to change provider).

chat_json() sends a system and user message, asks for a JSON object, validates the
reply with a Pydantic model, retries once on invalid JSON, and backs off on 429s.
"""
from __future__ import annotations

import json
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.settings import env

T = TypeVar("T", bound=BaseModel)
JSON_RULE = "Respond only with JSON."
MAX_RATE_LIMIT_RETRIES = 3


class LLMError(Exception):
    """The call failed for good (no key, bad JSON twice, or rate limited too often)."""


def model_name() -> str:
    return env("LLM_MODEL", "openai/gpt-oss-120b")


def make_client() -> Any:
    key = env("GROQ_API_KEY")
    if not key:
        raise LLMError("GROQ_API_KEY is not set in .env")
    from groq import Groq
    return Groq(api_key=key)


def _retry_after(err: Exception, attempt: int) -> float:
    """Seconds to wait: the retry-after header if present, else 2, 4, 8."""
    resp = getattr(err, "response", None)
    header = getattr(resp, "headers", {}).get("retry-after") if resp is not None else None
    try:
        return min(float(header), 60.0) if header else 2.0 ** (attempt + 1)
    except ValueError:
        return 2.0 ** (attempt + 1)


def _is_rate_limit(err: Exception) -> bool:
    return getattr(err, "status_code", None) == 429


def _complete(client: Any, messages: list[dict], temperature: float, max_tokens: int,
              reasoning_effort: str | None, sleep=time.sleep) -> str:
    kwargs: dict[str, Any] = dict(model=model_name(), messages=messages, temperature=temperature,
                                  max_completion_tokens=max_tokens, response_format={"type": "json_object"})
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001 - provider SDK errors vary
            if _is_rate_limit(e) and attempt < MAX_RATE_LIMIT_RETRIES:
                sleep(_retry_after(e, attempt))
                continue
            raise LLMError(f"{type(e).__name__}: {e}") from e
    raise LLMError("rate limited")


def parse_json(text: str, schema: type[T]) -> T:
    """Parse and validate. Raises ValueError on bad JSON or schema mismatch."""
    try:
        return schema.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(str(e)[:300]) from e


def chat_json(system: str, user: str, schema: type[T], *, client: Any = None, temperature: float = 0.2,
              max_tokens: int = 2048, reasoning_effort: str | None = "low", sleep=time.sleep) -> T:
    """One JSON call validated against `schema`. Retries once if the JSON is invalid."""
    client = client or make_client()
    if JSON_RULE not in system:
        system = f"{system}\n\n{JSON_RULE}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last_err = ""
    for _ in range(2):
        text = _complete(client, messages, temperature, max_tokens, reasoning_effort, sleep)
        try:
            return parse_json(text, schema)
        except ValueError as e:
            last_err = str(e)
            messages = messages[:2] + [
                {"role": "assistant", "content": text[:2000]},
                {"role": "user", "content": f"That was not valid JSON for the required shape ({last_err[:200]}). "
                                            f"{JSON_RULE}"},
            ]
    raise LLMError(f"invalid JSON after retry: {last_err}")
