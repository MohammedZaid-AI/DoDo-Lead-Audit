"""Listeners: pull public posts about payment pain from Hacker News and GitHub.

Shared pieces live here: the RawSignal shape, personal-data redaction, a polite
JSON getter (per-domain delay, identifying user agent), and payment-company filtering.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

import httpx

from app.settings import load_yaml, request_delay, user_agent_tag

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,}\d(?!\w|\.\d)")  # a full stop after the number is fine
TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class RawSignal:
    source: str  # hn | github
    url: str
    author: str | None
    title: str
    body: str
    created_at: datetime
    context: dict = field(default_factory=dict)  # matched phrase, story url, repo, ...


PHONE_MIN_DIGITS = 9  # a date like 2026-05-14 has 8 digits and is left alone


def _phone(m: re.Match) -> str:
    return "[phone removed]" if sum(ch.isdigit() for ch in m.group(0)) >= PHONE_MIN_DIGITS else m.group(0)


def redact(text: str) -> str:
    """Remove emails and phone numbers (we only store public post text and usernames)."""
    return PHONE_RE.sub(_phone, EMAIL_RE.sub("[email removed]", text or ""))


def html_to_text(html: str) -> str:
    import html as html_lib
    text = re.sub(r"<p>|<br\s*/?>", "\n", html or "", flags=re.I)
    return html_lib.unescape(TAG_RE.sub("", text)).strip()


def is_payment_company(name: str | None) -> bool:
    if not name:
        return False
    companies = {c.lower() for c in load_yaml("keywords").get("payment_companies", [])}
    return name.lower() in companies


class PoliteClient:
    """httpx client with the identifying user agent and a per-domain minimum delay."""

    def __init__(self, delay: float | None = None, headers: dict | None = None,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep) -> None:
        self.delay = request_delay() if delay is None else delay
        self.sleep = sleep
        self._last: dict[str, float] = {}
        self.client = httpx.Client(headers={"User-Agent": user_agent_tag(), **(headers or {})},
                                   timeout=30, transport=transport)

    def get_json(self, url: str, params: dict) -> dict:
        domain = urlparse(url).netloc
        gap = time.monotonic() - self._last.get(domain, 0.0)
        if gap < self.delay:
            self.sleep(self.delay - gap)
        self._last[domain] = time.monotonic()
        r = self.client.get(url, params=params)
        r.raise_for_status()
        return r.json()
