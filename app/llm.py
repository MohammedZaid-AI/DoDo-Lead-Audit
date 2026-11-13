"""Single wrapper for all LLM calls (Groq today; swap the client here to change provider).

chat_json() sends a system and user message, asks for a JSON object, validates the
reply with a Pydantic model, retries once on invalid JSON, and backs off on 429s.
"""
from __future__ import annotations

import json
import re
import time
from collections import deque
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.settings import env

T = TypeVar("T", bound=BaseModel)
JSON_RULE = "Respond only with JSON."
MAX_RATE_LIMIT_RETRIES = 3


class LLMError(Exception):
    """The call failed for good (no key, bad JSON twice, or rate limited too often)."""


class DailyLimitError(LLMError):
    """The provider's per-day token or request cap is used up. Retrying now is pointless."""


def model_name() -> str:
    return env("LLM_MODEL", "openai/gpt-oss-120b")


def make_client() -> Any:
    key = env("GROQ_API_KEY")
    if not key:
        raise LLMError("GROQ_API_KEY is not set in .env")
    from groq import Groq
    # Retries are handled here (429 backoff), so the SDK's own retries are off.
    return Groq(api_key=key, max_retries=0, timeout=60)


class TokenPacer:
    """Keeps requests under a tokens-per-minute budget (Groq free tier: 8000 TPM).

    Before each call it waits until the last 60 seconds of usage plus this call's
    estimate fit in the budget. Actual usage from the response replaces the estimate.
    """

    def __init__(self, tokens_per_minute: int, clock=time.monotonic, sleep=time.sleep) -> None:
        self.tpm = tokens_per_minute
        self.clock, self.sleep = clock, sleep
        self.window: deque[tuple[float, int]] = deque()

    def _used(self, now: float) -> int:
        while self.window and now - self.window[0][0] > 60:
            self.window.popleft()
        return sum(t for _, t in self.window)

    def wait_for(self, estimate: int) -> None:
        estimate = min(estimate, self.tpm)
        while True:
            now = self.clock()
            if self._used(now) + estimate <= self.tpm or not self.window:
                return
            self.sleep(max(0.5, 60 - (now - self.window[0][0]) + 0.1))

    def record(self, tokens: int) -> None:
        self.window.append((self.clock(), tokens))


_pacer: TokenPacer | None = None


def pacer() -> TokenPacer:
    global _pacer
    if _pacer is None:
        _pacer = TokenPacer(int(env("LLM_TOKENS_PER_MINUTE", "8000") or 8000))
    return _pacer


def estimate_tokens(messages: list[dict], max_tokens: int) -> int:
    """Rough: ~3.5 characters per token for the prompt, plus the likely reply and reasoning."""
    chars = sum(len(m["content"]) for m in messages)
    return int(chars / 3.5) + min(max_tokens, 800)


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


def _is_daily_limit(err: Exception) -> bool:
    msg = str(err).lower()
    return _is_rate_limit(err) and ("per day" in msg or "(tpd)" in msg or "(rpd)" in msg)


def _complete(client: Any, messages: list[dict], temperature: float, max_tokens: int,
              reasoning_effort: str | None, sleep=time.sleep, pace: TokenPacer | None = None) -> str:
    kwargs: dict[str, Any] = dict(model=model_name(), messages=messages, temperature=temperature,
                                  max_completion_tokens=max_tokens, response_format={"type": "json_object"})
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        estimate = estimate_tokens(messages, max_tokens)
        if pace:
            pace.wait_for(estimate)
        try:
            resp = client.chat.completions.create(**kwargs)
            if pace:
                usage = getattr(resp, "usage", None)
                pace.record(getattr(usage, "total_tokens", None) or estimate)
            return resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001 - provider SDK errors vary
            if _is_daily_limit(e):
                wait = re.search(r"try again in ([\w.]+)", str(e))
                raise DailyLimitError("daily LLM limit reached" + (f"; try again in {wait.group(1)}" if wait else "")) from e
            if _is_rate_limit(e) and attempt < MAX_RATE_LIMIT_RETRIES:
                if pace:
                    pace.record(estimate)  # the rejected attempt still counts against the window
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
    pace = pacer() if client is None else None  # injected test clients are not paced
    client = client or make_client()
    if JSON_RULE not in system:
        system = f"{system}\n\n{JSON_RULE}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last_err = ""
    for _ in range(2):
        text = _complete(client, messages, temperature, max_tokens, reasoning_effort, sleep, pace)
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
