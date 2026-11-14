"""GitHub listener: public issues that discuss payment providers, MoR, or selling abroad.

Uses the search API (GITHUB_TOKEN optional: 30 searches/min with it, 10/min without).
Skips payment companies' own orgs and collapses repeated titles within one repo
(spam and bot repos post the same issue many times).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from app.listen import PoliteClient, RawSignal, is_payment_company, redact
from app.settings import env, load_yaml

SEARCH = "https://api.github.com/search/issues"
REPO = "https://api.github.com/repos/{}"


def make_client() -> PoliteClient:
    token = env("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return PoliteClient(delay=2.5 if token else 6.5, headers=headers)


def repo_of(item: dict) -> str:
    return item["repository_url"].split("/repos/", 1)[1]


def item_to_signal(item: dict, phrase: str) -> RawSignal:
    repo = repo_of(item)
    return RawSignal(
        source="github", url=item["html_url"], author=(item.get("user") or {}).get("login"),
        title=item.get("title") or "", body=redact(item.get("body") or "")[:4000],
        created_at=datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),
        context={"phrase": phrase, "repo": repo, "repo_url": f"https://github.com/{repo}",
                 "author_association": item.get("author_association")},
    )


def _title_key(repo: str, title: str) -> str:
    return repo.lower() + "|" + re.sub(r"\W+", " ", title.lower()).strip()


def fetch_github(days: int = 7, phrases: list[str] | None = None, client: PoliteClient | None = None,
                 max_pages: int = 2) -> list[RawSignal]:
    """Issues created in the last `days` days matching any phrase."""
    phrases = phrases or load_yaml("keywords")["github_phrases"]
    client = client or make_client()
    since = (date.today() - timedelta(days=days)).isoformat()
    out: dict[str, RawSignal] = {}
    titles: set[str] = set()
    for phrase in phrases:
        for page in range(1, max_pages + 1):
            data = client.get_json(SEARCH, {"q": f'"{phrase}" is:issue is:public created:>={since}',
                                            "sort": "created", "order": "desc", "per_page": 100, "page": page})
            items = data.get("items", [])
            for item in items:
                repo = repo_of(item)
                owner = repo.split("/")[0]
                author = (item.get("user") or {}).get("login")
                if is_payment_company(owner) or is_payment_company(author) or (item.get("user") or {}).get("type") == "Bot":
                    continue
                key = _title_key(repo, item.get("title") or "")
                if key in titles or item["html_url"] in out:
                    continue
                titles.add(key)
                out[item["html_url"]] = item_to_signal(item, phrase)
            if len(items) < 100:
                break
    return list(out.values())


def repo_homepage(repo: str, client: PoliteClient | None = None) -> str | None:
    """The repo's public homepage field (often the product site), if set."""
    client = client or make_client()
    try:
        home = (client.get_json(REPO.format(repo), {}).get("homepage") or "").strip()
    except Exception:  # noqa: BLE001 - missing repo or rate limit: just no homepage
        return None
    if home and not home.startswith("http"):
        home = "https://" + home
    return home if home and "github.com" not in home else None


# ---------------------------------------------------------------- product site from the README

README = "https://api.github.com/repos/{}/readme"
MD_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\((https?://[^)\s]+)\)")
HTML_LINK_RE = re.compile(r"""<a\s[^>]*href=["'](https?://[^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
BARE_URL_RE = re.compile(r"(?<![(\"'=\[])\bhttps?://[^\s)<>\"'\]`|]+")
# Words that say a link IS the product site. Only checked in the link label or just before the link.
SITE_WORDS_RE = re.compile(r"\b(website|web ?site|live|demo|homepage|home ?page|try it|try now|visit|"
                           r"landing ?page|official site|sign ?up|get started)\b", re.I)
BEFORE_CHARS = 30
NOT_PRODUCT_HOSTS = re.compile(
    r"(^|\.)(github\.com|githubusercontent\.com|github\.io|gitlab\.com|shields\.io|badgen\.net|codecov\.io|"
    r"travis-ci\.\w+|circleci\.com|npmjs\.com|pypi\.org|crates\.io|docs\.rs|pkg\.go\.dev|readthedocs\.\w+|"
    r"discord\.(gg|com)|twitter\.com|x\.com|linkedin\.com|youtube\.com|youtu\.be|medium\.com|dev\.to|"
    r"producthunt\.com|reddit\.com|t\.me|wa\.me|instagram\.com|facebook\.com|vercel\.com|netlify\.com|"
    r"heroku\.com|render\.com|railway\.app|supabase\.(com|co)|firebase\.google\.com|google\.com|"
    r"stripe\.com|razorpay\.com|paypal\.com|paddle\.com|lemonsqueezy\.com|dodopayments\.com|cashfree\.com|"
    r"nextjs\.org|reactjs\.org|react\.dev|vuejs\.org|tailwindcss\.com|nodejs\.org|python\.org|"
    r"opensource\.org|creativecommons\.org|choosealicense\.com|buymeacoffee\.com|ko-fi\.com|patreon\.com|"
    r"linear\.app|notion\.so|figma\.com|loom\.com|calendly\.com|"
    r"localhost|127\.0\.0\.1|example\.(com|org|net)|yourdomain\.\w+|your-domain\.\w+)$", re.I)
ASSET_RE = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|mp4|pdf|zip)(\?|$)", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _links(line: str) -> list[tuple[str, str, int]]:
    """(label, url, start) for markdown, html and bare links. Image links are left out."""
    out = [(m.group(2), m.group(3), m.start()) for m in MD_LINK_RE.finditer(line) if not m.group(1)]
    out += [(re.sub(r"<[^>]+>", "", m.group(2)), m.group(1), m.start()) for m in HTML_LINK_RE.finditer(line)]
    taken = {u for _, u, _ in out} | {m.group(3) for m in MD_LINK_RE.finditer(line)}
    out += [("", m.group(0), m.start()) for m in BARE_URL_RE.finditer(line) if m.group(0) not in taken]
    return out


def readme_site(text: str, repo: str) -> str | None:
    """The product site linked from a README, or None when no link is clearly the product.

    A link counts when its label, or the few words right before it, say it is the site ('Live demo:',
    '[Website](...)', 'Try it'), or when its domain is named after the repo. A domain named after the owner
    counts only for a home page link (not an owner's blog article). Badges, registries, docs hosts, socials,
    tools, framework and payment sites are ignored. A wrong guess would audit the wrong company, so when
    unsure: None."""
    from app.audit.builder import _brand_label

    owner, _, name = repo.partition("/")
    repo_name, owner_name = _norm(name), _norm(owner)
    candidates: list[tuple[int, int, str]] = []
    for i, line in enumerate((text or "").splitlines()):
        for label, url, start in _links(line):
            url = url.rstrip(".,;:")
            m = re.match(r"https?://([^/:?#]+)([^?#]*)", url)
            if not m:
                continue
            host, path = m.group(1).lower().removeprefix("www."), m.group(2)
            if "." not in host or NOT_PRODUCT_HOSTS.search(host) or ASSET_RE.search(url):
                continue
            if re.search(r"badge|shield|license", label, re.I):
                continue
            brand = _norm(_brand_label(host))

            def named_after(n: str) -> bool:
                return len(n) >= 3 and len(brand) >= 3 and (n in brand or brand in n)

            score = 0
            if SITE_WORDS_RE.search(label) or SITE_WORDS_RE.search(line[max(0, start - BEFORE_CHARS):start]):
                score += 2
            if named_after(repo_name) or (named_after(owner_name) and path.strip("/") == ""):
                score += 2
            if score >= 2:
                candidates.append((-score, i, f"https://{host}"))
    return min(candidates)[2] if candidates else None


def repo_readme_site(repo: str, client: PoliteClient | None = None) -> str | None:
    """Fetch the README through the API and pick the product site from it."""
    import base64

    client = client or make_client()
    try:
        data = client.get_json(README.format(repo), {})
        text = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - no README, private, rate limited: just no site
        return None
    return readme_site(text, repo)
