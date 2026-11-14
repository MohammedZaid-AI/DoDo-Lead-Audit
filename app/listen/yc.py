"""Y Combinator startup directory: companies (not posts) whose public websites we then audit.

Reads the public browse pages like /companies/location/india. The company list is embedded in the page as
JSON (the data-page attribute), so no API key, login or search form is used. robots.txt is checked before
every page (it disallows /companies?..., the search page, which we never load) and requests are spaced out.

Stored per company: name, website, YC profile url, batch, tags, one-liner and public description.
Founder names and profiles are not read.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timezone
from typing import Callable

import httpx

from app.listen import RawSignal, redact
from app.settings import load_yaml, request_delay, user_agent_tag

BASE = "https://www.ycombinator.com"
DATA_RE = re.compile(r'data-page="([^"]+)"')
YEAR_RE = re.compile(r"(\d{4})")


def parse_page(page_html: str) -> dict:
    """The props of a directory page: companies, currentPage, totalPages, hasMore."""
    m = DATA_RE.search(page_html)
    if not m:
        raise ValueError("no company data on the page (the YC page layout may have changed)")
    return json.loads(html_lib.unescape(m.group(1))).get("props", {})


def batch_year(batch: str | None) -> int | None:
    m = YEAR_RE.search(batch or "")
    return int(m.group(1)) if m else None


def keep(c: dict, cfg: dict) -> bool:
    """Active software companies with a website from a recent batch."""
    if not c.get("website") or (c.get("ycdc_status") or "Active") != "Active":
        return False
    year = batch_year(c.get("batch_name"))
    if year is None or year < cfg.get("min_batch_year", 0):
        return False
    tags = {t.lower() for t in c.get("tags") or []}
    return bool(tags & set(cfg["include_tags"])) and not tags & set(cfg["exclude_tags"])


def to_signal(c: dict, list_path: str, now: datetime) -> RawSignal:
    one_liner = (c.get("one_liner") or "").strip()
    return RawSignal(
        source="yc", url=f"{BASE}/companies/{c['slug']}", author=None,
        title=f"{c.get('name', c['slug'])}: {one_liner}".strip(": "),
        body=redact(c.get("long_description") or "")[:2000], created_at=now,
        context={"name": c.get("name"), "website": c["website"], "batch": c.get("batch_name"),
                 "tags": c.get("tags") or [], "one_liner": one_liner, "list": list_path})


def fetch_directory(lists: list[str] | None = None, max_pages: int | None = None,
                    get: Callable[[str], str] | None = None, log=print) -> list[RawSignal]:
    """Companies from each directory list that pass the filter in config/yc.yaml."""
    cfg = load_yaml("yc")
    lists = lists or cfg["lists"]
    max_pages = max_pages or cfg.get("max_pages_per_list", 10)
    get = get or _polite_getter()
    now = datetime.now(timezone.utc)
    out: dict[str, RawSignal] = {}
    for path in lists:
        path = path.strip("/")
        seen = kept = 0
        for page in range(1, max_pages + 1):
            props = parse_page(get(f"{BASE}/companies/{path}" + (f"?page={page}" if page > 1 else "")))
            for c in props.get("companies", []):
                seen += 1
                url = f"{BASE}/companies/{c.get('slug')}"
                if keep(c, cfg) and url not in out:
                    out[url] = to_signal(c, path, now)
                    kept += 1
            if not props.get("hasMore") or page >= (props.get("totalPages") or page):
                break
        log(f"  yc {path}: {seen} companies, {kept} kept by the filter")
    return list(out.values())


def _polite_getter() -> Callable[[str], str]:
    """GET with robots.txt checked, the identifying user agent and the per-domain delay."""
    from app.detect.browser import Politeness

    ua = user_agent_tag()
    polite = Politeness(ua, request_delay())
    client = httpx.Client(headers={"User-Agent": ua}, timeout=30, follow_redirects=True)

    def get(url: str) -> str:
        reason = polite.check(url)
        if reason:
            raise PermissionError(f"{url}: {reason}")
        polite.wait(url)
        r = client.get(url)
        r.raise_for_status()
        return r.text

    return get
