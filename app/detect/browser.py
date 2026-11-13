"""Playwright page loader.

Renders a public page in headless Chromium and collects everything the detectors
need: final url, rendered html, script / iframe / link urls, every network request
url, visible text and the short labels of on-page controls (for currency switchers).

It is read-only: it never clicks, types, logs in or submits anything. It checks
robots.txt and waits REQUEST_DELAY_SECONDS between navigations to the same domain.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from app.settings import request_delay, user_agent_tag

PAGE_TIMEOUT_MS = 25_000
LAZY_WAIT_MS = 2_000

_COLLECT_JS = """
() => {
  const abs = (u) => { try { return new URL(u, location.href).href } catch { return u } };
  const short = (el) => (el.innerText || el.textContent || el.value || '').trim().slice(0, 40);
  const meta = document.querySelector('meta[name="description"], meta[property="og:description"]');
  return {
    title: document.title || '',
    meta_description: meta ? (meta.getAttribute('content') || '') : '',
    scripts: [...document.querySelectorAll('script[src]')].map(s => abs(s.getAttribute('src'))),
    iframes: [...document.querySelectorAll('iframe[src]')].map(f => abs(f.getAttribute('src'))),
    links: [...document.querySelectorAll('a[href]')].map(a => ({href: a.href, text: short(a)})),
    resources: [...document.querySelectorAll('link[href]')].map(l => abs(l.getAttribute('href'))),
    controls: [...document.querySelectorAll(
      'button, option, select, label, [role=tab], [role=switch], [role=radio], [role=option], [role=button]'
    )].map(short).filter(t => t && t.length <= 24),
  };
}
"""


@dataclass
class PageData:
    """Everything collected from one rendered page."""

    requested_url: str
    final_url: str = ""
    title: str = ""
    meta_description: str = ""
    html: str = ""
    text: str = ""
    scripts: list[str] = field(default_factory=list)
    iframes: list[str] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)  # {href, text}
    resources: list[str] = field(default_factory=list)  # <link href>
    network_urls: list[str] = field(default_factory=list)
    controls: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.html)

    def save(self, path_stem: Path) -> None:
        """Save as <stem>.html (rendered html) and <stem>.json (all other fields)."""
        data = asdict(self)
        path_stem.with_suffix(".html").write_text(data.pop("html"), encoding="utf-8")
        path_stem.with_suffix(".json").write_text(json.dumps(data, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path_stem: Path) -> "PageData":
        data = json.loads(path_stem.with_suffix(".json").read_text(encoding="utf-8"))
        data["html"] = path_stem.with_suffix(".html").read_text(encoding="utf-8")
        return cls(**data)


class Politeness:
    """robots.txt checks and a per-domain delay between navigations."""

    def __init__(self, ua: str, delay: float) -> None:
        self.ua = ua
        self.delay = delay
        self._robots: dict[str, RobotFileParser | str] = {}
        self._last: dict[str, float] = {}

    def check(self, url: str) -> str | None:
        """None if the url may be loaded, else the reason it may not."""
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            self._robots[base] = self._fetch_robots(base)
        rp = self._robots[base]
        if rp == "allow_all":
            return None
        if isinstance(rp, str):
            return rp
        return None if rp.can_fetch("DodoLeadAudit", url) else "blocked by robots.txt"

    def _fetch_robots(self, base: str) -> RobotFileParser | str:
        """Rules, 'allow_all' (no robots.txt, 4xx), or a reason string when it cannot be read.
        Fails closed: if robots.txt cannot be read (network error, 5xx) the site is skipped."""
        self.wait(base)
        try:
            r = httpx.get(f"{base}/robots.txt", headers={"User-Agent": self.ua},
                          timeout=10, follow_redirects=True)
        except httpx.HTTPError as e:
            return f"robots.txt unreachable ({type(e).__name__}); skipped to be safe"
        if r.status_code >= 500:
            return f"robots.txt returned {r.status_code}; skipped to be safe"
        if r.status_code >= 400:
            return "allow_all"
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp

    def wait(self, url: str) -> None:
        domain = urlparse(url).netloc
        gap = time.monotonic() - self._last.get(domain, 0.0)
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last[domain] = time.monotonic()


class Browser:
    """Context manager around one headless Chromium instance."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self.politeness: Politeness | None = None

    def __enter__(self) -> "Browser":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        probe = self._browser.new_page()
        base_ua = probe.evaluate("navigator.userAgent")
        probe.close()
        ua = f"{base_ua} {user_agent_tag()}"
        self._context = self._browser.new_context(user_agent=ua, viewport={"width": 1366, "height": 900})
        self.politeness = Politeness(ua, request_delay())
        return self

    def __exit__(self, *exc) -> None:
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def load(self, url: str) -> PageData:
        """Render one page. Never raises: errors are recorded on the result."""
        data = PageData(requested_url=url)
        assert self._context and self.politeness
        reason = self.politeness.check(url)
        if reason:
            data.error = reason
            return data
        self.politeness.wait(url)
        page = self._context.new_page()
        page.on("request", lambda req: data.network_urls.append(req.url))
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
            except PlaywrightTimeout:
                pass  # long-polling sites never go idle; use what loaded
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(LAZY_WAIT_MS)
            self._collect(page, data)
        except (PlaywrightTimeout, PlaywrightError) as e:
            data.error = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
        finally:
            page.close()
        return data

    @staticmethod
    def _collect(page, data: PageData) -> None:
        data.final_url = page.url
        data.html = page.content()
        data.text = page.inner_text("body")
        info = page.evaluate(_COLLECT_JS)
        for key in ("title", "meta_description", "scripts", "iframes", "links", "resources", "controls"):
            setattr(data, key, info[key])
        frame_urls = [f.url for f in page.frames if f.url and f.url != "about:blank"]
        data.iframes = sorted(set(data.iframes) | set(frame_urls[1:]))
