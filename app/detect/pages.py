"""Finds pricing / checkout / billing pages from a start page and loads them.

Loads the start url, the site root (if the start url is a deeper page), and up to
3 same-site candidate pages. Links that point at a known checkout domain are never
loaded; providers.py records them as high-confidence evidence instead.
"""
from __future__ import annotations

from urllib.parse import urldefrag, urlparse

from app.detect.browser import Browser, PageData
from app.detect.providers import fingerprints

KEYWORDS = ["pricing", "plans", "price", "billing", "upgrade", "checkout", "buy", "subscribe", "credits"]
SKIP_WORDS = ["login", "log-in", "signin", "sign-in", "signup", "sign-up", "register", "auth",
              "logout", "account", "blog", "docs", "changelog", "careers", "privacy", "terms"]
SKIP_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".svg", ".zip", ".mp4")


def root_domain(url: str) -> str:
    """Last two labels of the host (good enough for .com/.ai/.io style domains)."""
    host = urlparse(url).netloc.lower().split(":")[0]
    parts = host.split(".")
    if len(parts) > 2 and parts[-2] in {"co", "com", "org", "net"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _is_checkout_url(url: str) -> bool:
    low = url.lower()
    for fp in fingerprints().values():
        if any(p.lower() in low for p in fp.get("checkout_patterns", []) + fp.get("patterns", [])):
            return True
    return False


def _norm(url: str) -> str:
    return urldefrag(url)[0].rstrip("/")


def find_candidate_pages(page: PageData, limit: int = 3) -> list[str]:
    """Same-site links whose url or text mentions pricing-type keywords, best first."""
    base = page.final_url or page.requested_url
    site = root_domain(base)
    exclude = {_norm(base), _norm(page.requested_url)}
    scored: dict[str, int] = {}
    for link in page.links:
        href = _norm(link.get("href", ""))
        p = urlparse(href)
        if p.scheme not in ("http", "https") or root_domain(href) != site or href in exclude:
            continue
        path = p.path.lower()
        text = (link.get("text") or "").lower()
        if path.endswith(SKIP_EXT) or any(w in path for w in SKIP_WORDS) or _is_checkout_url(href):
            continue
        score = sum(3 for k in KEYWORDS if k in path) + sum(1 for k in KEYWORDS if k in text)
        if score:
            scored[href] = max(score, scored.get(href, 0))
    ranked = sorted(scored, key=lambda h: (-scored[h], len(h)))
    return ranked[:limit]


def site_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def inspect_site(url: str, browser: Browser, max_extra: int = 3) -> list[PageData]:
    """Load the start page plus root and candidate pages. Returns every attempt."""
    first = browser.load(url)
    pages = [first]
    queue: list[str] = []
    if _norm(site_root(url)) != _norm(url):
        queue.append(site_root(url))
    if first.ok:
        queue += find_candidate_pages(first, limit=max_extra)
    seen = {_norm(url), _norm(first.final_url or url)}
    for target in queue:
        if len(pages) > max_extra or _norm(target) in seen:
            continue
        seen.add(_norm(target))
        pages.append(browser.load(target))
    return pages
