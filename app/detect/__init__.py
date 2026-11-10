"""Detection engine: load a site's public pages and report its payment setup."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.detect.browser import Browser, PageData
from app.detect.pages import inspect_site
from app.detect.pricing import PricingResult, analyze_pricing
from app.detect.providers import ProviderResult, detect_providers


@dataclass
class DetectionResult:
    url: str
    pages_checked: list[str]
    page_errors: dict[str, str]
    title: str
    meta_description: str
    provider: ProviderResult
    pricing: PricingResult
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


def analyze_pages(url: str, pages: list[PageData]) -> DetectionResult:
    """Combine provider and pricing analysis over already-loaded pages."""
    ok = [p for p in pages if p.ok]
    text = "\n".join(p.text for p in ok)
    controls = [c for p in ok for c in p.controls]
    first = ok[0] if ok else None
    return DetectionResult(
        url=url,
        pages_checked=[p.final_url or p.requested_url for p in ok],
        page_errors={p.requested_url: p.error for p in pages if p.error},
        title=first.title if first else "",
        meta_description=first.meta_description if first else "",
        provider=detect_providers(ok),
        pricing=analyze_pricing(text, controls),
    )


def detect(url: str, browser: Browser) -> tuple[DetectionResult, list[PageData]]:
    """Load the site and analyze it. Returns the result and the raw pages."""
    pages = inspect_site(url, browser)
    return analyze_pages(url, pages), pages
