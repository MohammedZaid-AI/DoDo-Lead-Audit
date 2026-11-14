"""Glue: listen > classify > product url > detect > score > audit > leads.csv."""
from __future__ import annotations

import csv
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import select

from app import db
from app.audit.builder import build_audit, domain_of
from app.audit.render import save_audit
from app.detect import analyze_pages, detect
from app.detect.browser import Browser, PageData
from app.listen import RawSignal
from app.listen.classify import PAYMENT_TERMS, classify, worth_classifying
from app.listen.github import fetch_github, repo_homepage
from app.listen.hn import fetch_hn
from app.llm import DailyLimitError
from app.score import facts_for, score_lead
from app.settings import OUTPUT_DIR

SOURCES: dict[str, Callable[..., list[RawSignal]]] = {"hn": fetch_hn, "github": fetch_github}


@dataclass
class ListenStats:
    fetched: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    new: int = 0
    classified: int = 0
    pain: int = 0
    failed: int = 0
    skipped: int = 0
    stopped: str = ""


def to_raw(sig: db.Signal) -> RawSignal:
    return RawSignal(sig.source, sig.url, sig.author, sig.title or "", sig.body or "", sig.created_at, sig.context or {})


def listen(days: int, sources: list[str], do_classify: bool = True, log=print) -> ListenStats:
    """Fetch from each source, store new signals, then classify pending ones."""
    stats = ListenStats()
    with db.session() as s:
        for name in sources:
            log(f"fetching {name} (last {days} days)...")
            try:
                raws = SOURCES[name](days=days)
            except Exception as e:  # noqa: BLE001 - one source failing should not stop the other
                stats.errors[name] = f"{type(e).__name__}: {e}"[:200]
                continue
            stats.fetched[name] = len(raws)
            stats.new += db.store_signals(s, raws)
    if do_classify:
        classify_pending(stats, log=log)
    return stats


def classify_pending(stats: ListenStats | None = None, limit: int | None = None, log=print) -> ListenStats:
    """Classify pending and previously failed signals; fill product_url from the repo homepage."""
    stats = stats or ListenStats()
    with db.session() as s:
        pending = s.exec(select(db.Signal).where(db.Signal.status.in_(["pending", "failed"]))).all()
        rows = []
        for sig in pending:
            if worth_classifying(to_raw(sig)):
                rows.append(sig)
            else:
                sig.status, sig.is_payment_pain = "skipped", False
                sig.classification = {"one_line_reason": "Pre-filter: no payment provider plus pain or decision terms."}
                s.add(sig)
                stats.skipped += 1
        s.commit()
        log(f"  {len(rows)} posts to classify, {stats.skipped} skipped by the keyword pre-filter")
        for i, sig in enumerate(rows[:limit] if limit else rows, 1):
            try:
                result = classify(to_raw(sig))
            except DailyLimitError as e:
                stats.stopped = str(e)
                log(f"  stopped: {e}. Run 'python cli.py classify' later to continue ({len(rows) - i + 1} left).")
                break
            if result is None:
                sig.status = "failed"
                stats.failed += 1
            else:
                if result.is_payment_pain and not result.product_url and sig.source == "github":
                    result.product_url = repo_homepage(sig.context.get("repo", ""))
                sig.classification = result.model_dump()
                sig.is_payment_pain = result.is_payment_pain
                sig.pain_type, sig.urgency, sig.product_url = result.pain_type, result.urgency, result.product_url
                sig.status = "classified"
                stats.classified += 1
                stats.pain += int(result.is_payment_pain)
            s.add(sig)
            s.commit()
            if i % 5 == 0:
                log(f"  classified {i}/{len(rows)}")
    return stats


def mark_pain_for_recheck(all_classified: bool = False) -> int:
    """Send posts back to the classifier after a prompt change: pain only, or every classified post."""
    with db.session() as s:
        q = select(db.Signal).where(db.Signal.status == "classified")
        if not all_classified:
            q = q.where(db.Signal.is_payment_pain == True)  # noqa: E712
        rows = s.exec(q).all()
        for r in rows:
            r.status = "pending"
            s.add(r)
        s.commit()
        return len(rows)


# ---------------------------------------------------------------- M5: product url > detect > score > audit

SKIP_HOSTS = re.compile(r"(^|\.)(github\.com|gitlab\.com|npmjs\.com|pypi\.org|vercel\.com|netlify\.com|"
                        r"stripe\.com|paddle\.com|lemonsqueezy\.com|razorpay\.com|paypal\.com|dodopayments\.com|"
                        r"ycombinator\.com|localhost|example\.com)$")
CACHE_DIR = OUTPUT_DIR / "cache" / "pages"
CACHE_HOURS = 24


@dataclass
class Lead:
    domain: str
    url: str
    signal_id: int
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    detection: object = None
    audit_path: str = ""
    audit_id: int | None = None
    opener: str = ""


@dataclass
class RunStats:
    candidates: int = 0
    no_url: int = 0
    sites: int = 0
    site_errors: int = 0
    scored: int = 0
    audited: int = 0
    openers: int = 0
    csv: str = ""


def site_domain(url: str) -> str:
    return domain_of(url if url.startswith("http") else "https://" + url)


def product_url_for(sig: db.Signal, homepage: Callable[[str], str | None] = repo_homepage) -> str | None:
    """The product site for a pain signal: the classifier's url, else the GitHub repo homepage (checked once)."""
    url = sig.product_url
    ctx = sig.context or {}
    if not url and sig.source == "github" and ctx.get("repo") and not ctx.get("homepage_checked"):
        url = homepage(ctx["repo"])
        sig.context = {**ctx, "homepage_checked": True}
        sig.product_url = url
    if not url:
        return None
    url = url if url.startswith("http") else "https://" + url
    host = site_domain(url)
    return None if not host or SKIP_HOSTS.search(host) else url


def signal_quote(sig: db.Signal, limit: int = 240) -> str:
    """The sentence from the post that talks about payments, else the title. Bodies were redacted at fetch."""
    body = re.sub(r"\*\*|__|`", "", sig.body or "")  # drop Markdown emphasis and code marks
    body = re.sub(r"\s+", " ", body).strip()
    for sent in re.split(r"(?<=[.!?])\s+", body):
        if PAYMENT_TERMS.search(sent) and len(sent) > 20:
            return sent[:limit].rstrip() + ("..." if len(sent) > limit else "")
    return (sig.title or "")[:limit]


def signal_dict(sig: db.Signal) -> dict:
    return {"id": sig.id, "source": sig.source, "url": sig.url, "author": sig.author, "title": sig.title,
            "posted": sig.created_at.date().isoformat(), "quote": signal_quote(sig), "pain_type": sig.pain_type,
            "urgency": sig.urgency, "body": (sig.body or "")[:400]}


def _slug(domain: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", domain.lower()).strip("_")


def _cached_pages(slug: str) -> list[PageData] | None:
    stems = sorted({p.with_suffix("") for p in CACHE_DIR.glob(f"{slug}__*.json")})
    if not stems or time.time() - stems[0].with_suffix(".json").stat().st_mtime > CACHE_HOURS * 3600:
        return None
    return [PageData.load(s) for s in stems]


def detect_sites(urls: dict[str, str], log=print) -> dict:
    """Detect each domain once. Rendered pages are cached for 24 hours so a re-run does not reload sites."""
    results: dict = {}
    todo: dict[str, str] = {}
    for domain, url in urls.items():
        pages = _cached_pages(_slug(domain))
        if pages:
            results[domain] = analyze_pages(url, pages)
        else:
            todo[domain] = url
    if todo:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with Browser() as browser:
            for i, (domain, url) in enumerate(todo.items(), 1):
                log(f"  [{i}/{len(todo)}] loading {url}")
                result, pages = detect(url, browser)
                for j, page in enumerate(p for p in pages if p.ok):
                    page.save(CACHE_DIR / f"{_slug(domain)}__{j}")
                results[domain] = result
    return results


def pick_leads(by_domain: dict[str, list], urls: dict[str, str], detections: dict, now: datetime,
               stats: RunStats, log=print) -> list[Lead]:
    """Score every (site, post) pair; keep each site's best post. Sorted best first."""
    leads: list[Lead] = []
    for domain, det in detections.items():
        if not det.pages_checked:
            stats.site_errors += 1
            log(f"  skipped {domain}: no page loaded ({'; '.join(det.page_errors.values())[:120]})")
            continue
        best = max(((sig, score_lead(facts_for(sig, det), now=now)) for sig in by_domain[domain]),
                   key=lambda pair: pair[1].value)
        leads.append(Lead(domain, urls[domain], best[0].id, best[1].value, best[1].reasons, det))
    leads.sort(key=lambda l: (-l.score, l.domain))
    stats.scored = len(leads)
    return leads


def run(days: int = 7, top: int = 20, do_listen: bool = True, sources: list[str] | None = None,
        with_opener: bool = True, csv_path: Path = OUTPUT_DIR / "leads.csv", now: datetime | None = None,
        log=print) -> tuple[RunStats, list[Lead]]:
    """listen > classify > product url > detect > score > audit the top N > leads.csv."""
    now = now or datetime.now(timezone.utc)
    stats = RunStats()
    if do_listen:
        listen(days, sources or ["hn", "github"], True, log=log)

    since = now - timedelta(days=days)
    with db.session() as s:
        sigs = s.exec(select(db.Signal).where(db.Signal.is_payment_pain == True,  # noqa: E712
                                              db.Signal.created_at >= since)).all()
        stats.candidates = len(sigs)
        log(f"{len(sigs)} payment pain posts from the last {days} days; finding product sites...")
        by_domain: dict[str, list[db.Signal]] = {}
        urls: dict[str, str] = {}
        for sig in sigs:
            url = product_url_for(sig)
            s.add(sig)
            if not url:
                stats.no_url += 1
                continue
            d = site_domain(url)
            by_domain.setdefault(d, []).append(sig)
            urls.setdefault(d, url)
        s.commit()
        log(f"{len(urls)} distinct product sites ({stats.no_url} posts had no usable product url)")

        detections = detect_sites(urls, log=log)
        stats.sites = len(detections)
        leads = pick_leads(by_domain, urls, detections, now, stats, log)
        chosen = leads[:top]

        log(f"auditing the top {len(chosen)} of {len(leads)} scored sites...")
        for i, lead in enumerate(chosen, 1):
            sig = s.get(db.Signal, lead.signal_id)
            audit = build_audit(lead.detection, signal_dict(sig), with_opener)
            path = save_audit(audit)
            company = db.upsert_company(s, audit.company_name, audit.url, audit.domain)
            db.save_detection(s, company.id, lead.detection)
            row = db.save_audit(s, company.id, audit, str(path), signal_id=sig.id, score=lead.score,
                                extra={"score": lead.score, "score_reasons": lead.reasons})
            sig.company_id = company.id
            s.add(sig)
            s.commit()
            lead.audit_path, lead.audit_id, lead.opener = str(path), row.id, audit.opener
            stats.audited += 1
            stats.openers += int(bool(audit.opener))
            note = "ok" if audit.opener else audit.opener_note[:90]
            log(f"  [{i}/{len(chosen)}] score {lead.score:g}  {lead.domain}  opener: {note}")
        # lead audits from earlier runs that are not in this run's top N: drop them so the list is current.
        # Audits made by hand with 'audit <url>' have no signal and are kept.
        kept = {l.audit_id for l in chosen}
        for row in s.exec(select(db.Audit).where(db.Audit.signal_id != None)).all():  # noqa: E711
            if row.id not in kept:
                s.delete(row)
        s.commit()
        stats.csv = str(export_leads(s, chosen, csv_path))
    return stats, chosen


def export_leads(s, leads: list[Lead], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["rank", "score", "company", "product_url", "provider", "merchant_of_record", "currency_mode",
                    "detection_confidence", "pain_type", "urgency", "source", "post_url", "posted",
                    "score_reasons", "opener", "audit_file"])
        for rank, lead in enumerate(leads, 1):
            sig, det = s.get(db.Signal, lead.signal_id), lead.detection
            mor = {True: "yes", False: "no", None: "not verified"}[det.provider.has_mor]
            w.writerow([rank, lead.score, lead.domain, lead.url, det.provider.primary or "not verified", mor,
                        det.pricing.currency_mode, det.provider.confidence, sig.pain_type, sig.urgency,
                        sig.source, sig.url, sig.created_at.date(), "; ".join(lead.reasons), lead.opener,
                        lead.audit_path])
    return path
