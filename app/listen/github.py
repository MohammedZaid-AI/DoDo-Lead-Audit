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
