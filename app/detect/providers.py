"""Matches payment provider fingerprints against collected page data.

Confidence by where a pattern matched:
  high   - script src, iframe src, network request url, or an <a href> to a checkout url
  medium - any other <a href> or <link href> (preconnect, preload, plain link),
           or a visible statement such as "We use Stripe for secure payments"
  low    - only in inline html / page text (blog footers, comparison tables)
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from app.detect.browser import PageData
from app.detect.textutil import is_declaration, is_negated, sentences
from app.settings import load_yaml

RANK = {"high": 3, "medium": 2, "low": 1}
MAX_EVIDENCE_PER_KIND = 3


@dataclass
class Evidence:
    provider: str
    pattern: str
    location: str  # script | iframe | network | checkout_link | link | resource | inline_html
    matched: str  # the matched url, or a snippet around the match
    page_url: str
    confidence: str


@dataclass
class ProviderResult:
    primary: str | None
    has_mor: bool | None
    confidence: str  # of the primary match; "low" when nothing matched
    providers: dict[str, str] = field(default_factory=dict)  # name -> best confidence
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def fingerprints() -> dict[str, dict]:
    return load_yaml("providers")


def _site_domain(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _is_own_site(provider_patterns: list[str], page_url: str) -> bool:
    """Skip matches when we are auditing the provider's own website."""
    dom = _site_domain(page_url)
    return any(dom and dom in p.lower() for p in provider_patterns)


def _first_match(value: str, patterns: list[str], low: str | None = None) -> str | None:
    low = value.lower() if low is None else low
    return next((p for p in patterns if p.lower() in low), None)


def _snippet(html: str, pattern: str, width: int = 60, low: str | None = None) -> str:
    i = (html.lower() if low is None else low).find(pattern.lower())
    return " ".join(html[max(0, i - width): i + len(pattern) + width].split())


def match_page(page: PageData, fps: dict[str, dict] | None = None) -> list[Evidence]:
    """All fingerprint matches on one page, capped per provider and location."""
    fps = fps or fingerprints()
    url = page.final_url or page.requested_url
    sents = sentences(page.text)
    found: list[Evidence] = []
    html_low: str | None = None  # lowercased once, only if a provider needs the inline html check
    for name, fp in fps.items():
        patterns: list[str] = fp.get("patterns", [])
        checkout: list[str] = fp.get("checkout_patterns", [])
        if _is_own_site(patterns, url):
            continue
        sources = [
            ("script", page.scripts, "high"),
            ("iframe", page.iframes, "high"),
            ("network", page.network_urls, "high"),
            ("resource", page.resources, "medium"),
        ]
        for location, values, conf in sources:
            found += _match_values(name, location, values, patterns, url, conf)
        hrefs = [l["href"] for l in page.links]
        checkout_hits = _match_values(name, "checkout_link", hrefs, checkout, url, "high")
        found += checkout_hits
        if not checkout_hits:
            found += _match_values(name, "link", hrefs, patterns, url, "medium")
        found += _match_text(name, fp.get("names", []), sents, url)
        if not any(e.provider == name for e in found):
            if html_low is None:
                html_low = page.html.lower()
            pat = _first_match(page.html, patterns, html_low)
            if pat:
                found.append(Evidence(name, pat, "inline_html", _snippet(page.html, pat, low=html_low), url, "low"))
    return found


def _match_text(name: str, brand_names: list[str], sents: list[str], page_url: str) -> list[Evidence]:
    """Brand names in visible text. 'We use Stripe ...' is medium, other mentions low.
    Negated sentences ('we don't support PayPal') are ignored."""
    out: list[Evidence] = []
    for i, s in enumerate(sents):
        brand = next((b for b in brand_names if re.search(rf"(?<![\w.]){re.escape(b)}(?![\w])", s)), None)
        if not brand or is_negated(sents, i):
            continue
        if is_declaration(s):
            out.append(Evidence(name, brand, "text_statement", s[:300], page_url, "medium"))
        elif not any(e.location == "text" for e in out):
            out.append(Evidence(name, brand, "text", s[:300], page_url, "low"))
        if len(out) >= MAX_EVIDENCE_PER_KIND:
            break
    return out


def _match_values(name: str, location: str, values: list[str], patterns: list[str],
                  page_url: str, conf: str) -> list[Evidence]:
    out: list[Evidence] = []
    seen: set[str] = set()
    for v in values:
        pat = _first_match(v, patterns)
        if pat and v not in seen:
            seen.add(v)
            out.append(Evidence(name, pat, location, v[:300], page_url, conf))
            if len(out) >= MAX_EVIDENCE_PER_KIND:
                break
    return out


def detect_providers(pages: list[PageData], fps: dict[str, dict] | None = None) -> ProviderResult:
    """Merge matches across pages and pick the primary provider."""
    fps = fps or fingerprints()
    evidence = [e for p in pages if p.ok for e in match_page(p, fps)]
    best: dict[str, str] = {}
    counts: dict[str, int] = {}
    for e in evidence:
        counts[e.provider] = counts.get(e.provider, 0) + 1
        if RANK[e.confidence] > RANK.get(best.get(e.provider, ""), 0):
            best[e.provider] = e.confidence
    # Low-only matches (logo walls, comparison tables) are kept as evidence but
    # never reported as the primary provider.
    strong = {n: c for n, c in best.items() if c != "low"}
    if not strong:
        return ProviderResult(primary=None, has_mor=None, confidence="low", providers=best, evidence=evidence)
    primary = max(strong, key=lambda n: (RANK[strong[n]], counts[n]))
    return ProviderResult(primary, bool(fps[primary].get("is_mor")), strong[primary], best, evidence)
