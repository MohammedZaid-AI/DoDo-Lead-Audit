"""Hacker News listener via the Algolia HN Search API (no key needed).

Searches each phrase as an exact phrase over stories and comments from the last N days.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from app.listen import PoliteClient, RawSignal, html_to_text, is_payment_company, redact
from app.settings import load_yaml

API = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM = "https://news.ycombinator.com/item?id={}"


def hit_to_signal(hit: dict, phrase: str) -> RawSignal:
    is_comment = bool(hit.get("comment_text"))
    body = html_to_text(hit.get("comment_text") or hit.get("story_text") or "")
    title = hit.get("title") or hit.get("story_title") or ""
    return RawSignal(
        source="hn", url=HN_ITEM.format(hit["objectID"]), author=hit.get("author"),
        title=("Comment on: " if is_comment else "") + title, body=redact(body)[:4000],
        created_at=datetime.fromtimestamp(int(hit["created_at_i"]), tz=timezone.utc),
        context={"phrase": phrase, "kind": "comment" if is_comment else "story",
                 "story_url": hit.get("url") or hit.get("story_url"),
                 "story": HN_ITEM.format(hit["story_id"]) if hit.get("story_id") else None},
    )


def fetch_hn(days: int = 7, phrases: list[str] | None = None, client: PoliteClient | None = None) -> list[RawSignal]:
    """Stories and comments matching any phrase in the last `days` days."""
    phrases = phrases or load_yaml("keywords")["phrases"]
    client = client or PoliteClient()
    since = int(time.time()) - days * 86400
    out: dict[str, RawSignal] = {}
    for phrase in phrases:
        data = client.get_json(API, {"query": f'"{phrase}"', "tags": "(story,comment)",
                                     "numericFilters": f"created_at_i>{since}", "hitsPerPage": 100})
        for hit in data.get("hits", []):
            if is_payment_company(hit.get("author")):
                continue
            sig = hit_to_signal(hit, phrase)
            if sig.body or sig.title:
                out.setdefault(sig.url, sig)
    return list(out.values())
