"""Command line entry point for Dodo Lead Audit."""
from __future__ import annotations

import json
import re
from pathlib import Path

import typer

import csv

from sqlmodel import select

from app import db, pipeline
from app.audit.builder import build_audit
from app.audit.render import save_audit
from app.detect import DetectionResult, analyze_pages, detect
from app.detect.browser import Browser, PageData
from app.settings import OUTPUT_DIR, ROOT

cli = typer.Typer(add_completion=False, help="Find and audit leads for Dodo Payments. Read only.")
FIXTURE_DIR = ROOT / "tests" / "fixtures"


def slugify(url: str) -> str:
    s = re.sub(r"^https?://(www\.)?", "", url.lower())
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:60]


def print_summary(r: DetectionResult) -> None:
    p, pr = r.provider, r.pricing
    mor = {True: "yes", False: "no", None: "not verified"}[p.has_mor]
    typer.secho(f"\n== {r.url}", bold=True)
    typer.echo(f"  title:      {r.title[:90]}")
    typer.echo(f"  pages:      {', '.join(r.pages_checked) or 'none loaded'}")
    for u, err in r.page_errors.items():
        typer.secho(f"  error:      {u} -> {err}", fg="yellow")
    typer.echo(f"  provider:   {p.primary or 'not verified'} (confidence {p.confidence}); all: {p.providers or '-'}")
    typer.echo(f"  MoR:        {mor}")
    for e in p.evidence[:8]:
        typer.echo(f"    evidence: [{e.confidence}] {e.provider} '{e.pattern}' in {e.location}: {e.matched[:100]}")
    typer.echo(f"  currencies: {pr.currencies_seen}  mode: {pr.currency_mode}  switcher: {pr.has_currency_switcher}")
    for s in pr.price_samples[:8]:
        typer.echo(f"    price:    {s.raw_text!r} -> {s.amount}{'-' + str(s.amount_max) if s.amount_max else ''} {s.currency} per {s.period}")
    typer.echo(f"  tax hints:  {pr.tax_hints or '-'}")
    typer.echo(f"  local pay:  {pr.local_methods or '-'}")


def _fixture_pages(url: str) -> list[PageData]:
    stems = sorted({p.with_suffix("") for p in FIXTURE_DIR.glob(f"{slugify(url)}__*.json")})
    if not stems:
        raise typer.BadParameter(f"no fixtures for {url}; run once with --save-fixtures")
    return [PageData.load(s) for s in stems]


def audit_detection(result: DetectionResult, signal: dict | None = None, with_opener: bool = True) -> Path:
    """Build, render and store the audit for one detection. Returns the html path."""
    audit = build_audit(result, signal, with_opener)
    path = save_audit(audit)
    with db.session() as s:
        company = db.upsert_company(s, audit.company_name, audit.url, audit.domain)
        db.save_detection(s, company.id, result)
        db.save_audit(s, company.id, audit, str(path))
    typer.echo(f"  issues:     {[i.key for i in audit.issues] or '-'}")
    if audit.opener:
        typer.echo(f"  opener ({len(audit.opener.split())} words): {audit.opener}")
    else:
        typer.secho(f"  opener:     {audit.opener_note}", fg="yellow")
    typer.secho(f"  audit:      {path}", fg="green")
    return path


def _run_urls(urls: list[str], save_fixtures: bool, from_fixtures: bool = False, opener: bool = True) -> list[DetectionResult]:
    results = []
    out = OUTPUT_DIR / "detections"
    out.mkdir(parents=True, exist_ok=True)
    if from_fixtures:
        for url in urls:
            result = analyze_pages(url, _fixture_pages(url))
            results.append(result)
            print_summary(result)
            audit_detection(result, with_opener=opener)
        return results
    with Browser() as browser:
        for url in urls:
            result, pages = detect(url, browser)
            results.append(result)
            print_summary(result)
            slug = slugify(url)
            (out / f"{slug}.json").write_text(json.dumps(result.to_dict(), indent=1), encoding="utf-8")
            if save_fixtures:
                FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
                for i, page in enumerate(p for p in pages if p.ok):
                    page.save(FIXTURE_DIR / f"{slug}__{i}")
            audit_detection(result, with_opener=opener)
    return results


@cli.command()
def audit(url: str, save_fixtures: bool = typer.Option(False, help="Save rendered pages to tests/fixtures"),
          from_fixtures: bool = typer.Option(False, help="Use saved fixtures instead of loading the site"),
                  opener: bool = typer.Option(True, help="Draft the LLM opener (needs GROQ_API_KEY)")) -> None:
    """Run detection and audit on one url. Saves html to output/audits/."""
    _run_urls([url], save_fixtures, from_fixtures, opener)


@cli.command("audit-file")
def audit_file(path: Path, save_fixtures: bool = typer.Option(False, help="Save rendered pages to tests/fixtures"),
               from_fixtures: bool = typer.Option(False, help="Use saved fixtures instead of loading the sites"),
                       opener: bool = typer.Option(True, help="Draft the LLM opener (needs GROQ_API_KEY)")) -> None:
    """Run detection and audit on every url in a text file (one per line)."""
    urls = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    _run_urls(urls, save_fixtures, from_fixtures, opener)


@cli.command()
def listen(days: int = typer.Option(7, help="How many days back to search"),
           sources: str = typer.Option("hn,github", help="Comma separated: hn, github"),
           classify: bool = typer.Option(True, help="Classify new posts with the LLM")) -> None:
    """Pull posts from Hacker News and GitHub, store new ones, classify them."""
    stats = pipeline.listen(days, [x.strip() for x in sources.split(",") if x.strip()], classify, log=typer.echo)
    for name, n in stats.fetched.items():
        typer.echo(f"  {name}: {n} posts fetched")
    for name, err in stats.errors.items():
        typer.secho(f"  {name} failed: {err}", fg="red")
    typer.echo(f"  new signals stored: {stats.new}")
    if classify:
        typer.echo(f"  skipped by pre-filter: {stats.skipped}  classified: {stats.classified}  payment pain: {stats.pain}  failed: {stats.failed}")
    with db.session() as s:
        total = len(s.exec(select(db.Signal)).all())
    typer.secho(f"  total signals in database: {total}", bold=True)


@cli.command()
def run(days: int = typer.Option(7, help="How many days back to search"),
        top: int = typer.Option(20, help="How many leads to audit and export"),
        listen_first: bool = typer.Option(True, "--listen/--no-listen", help="Fetch new posts first (else use the database)"),
        sources: str = typer.Option("hn,github", help="Comma separated: hn, github"),
        opener: bool = typer.Option(True, help="Draft the LLM opener for each audit"),
        yc: int = typer.Option(0, help="YC directory companies to newly check this run (0 = off; see config/yc.yaml)"),
        yc_lists: str = typer.Option("", help="Comma separated YC lists, e.g. location/india,industry/saas")) -> None:
    """Listen, classify, find product sites, detect, score, audit the top N, export output/leads.csv."""
    st, leads = pipeline.run(days, top, listen_first, [x.strip() for x in sources.split(",") if x.strip()],
                             opener, log=typer.echo, yc=yc,
                             yc_lists=[x.strip() for x in yc_lists.split(",") if x.strip()] or None)
    typer.secho("\nTop leads", bold=True)
    for i, l in enumerate(leads, 1):
        det = l.detection
        typer.echo(f"  {i:>2}. {l.score:>4g}  {l.domain:32} {det.provider.primary or 'not verified':14} "
                   f"{det.pricing.currency_mode:15} {'; '.join(l.reasons)[:90]}")
    typer.echo(f"\n  pain posts: {st.candidates}  without product url: {st.no_url}  sites: {st.sites}  "
               f"unreachable: {st.site_errors}  audited: {st.audited}  openers ok: {st.openers}")
    if st.yc_sites:
        typer.echo(f"  YC companies scored: {st.yc_sites}  newly loaded: {st.yc_new}")
    typer.secho(f"  leads csv: {st.csv}", fg="green")


@cli.command()
def serve(host: str = typer.Option("127.0.0.1", help="Address to bind (local only by default)"),
          port: int = typer.Option(8000, help="Port")) -> None:
    """Start the dashboard at http://127.0.0.1:8000."""
    import uvicorn
    typer.secho(f"Dashboard: http://{host}:{port}  (Ctrl+C to stop)", fg="green")
    uvicorn.run("app.web.main:app", host=host, port=port, log_level="warning")


@cli.command()
def redraft(all_leads: bool = typer.Option(False, "--all", help="Redraft every lead, not only failed openers")) -> None:
    """Draft openers again for leads whose opener failed its checks. Keeps scores and 'reviewed'."""
    tried, ok = pipeline.redraft(only_missing=not all_leads, log=typer.echo)
    typer.secho(f"  redrafted: {tried}  openers ok: {ok}", bold=True)


@cli.command("classify")
def classify_cmd(recheck_pain: bool = typer.Option(False, help="Re-check posts already marked as payment pain"),
                 recheck_all: bool = typer.Option(False, help="Re-check every classified post (after a prompt change)"),
                 limit: int = typer.Option(0, help="Classify at most this many (0 = all)")) -> None:
    """Classify stored signals that are pending or failed (no fetching)."""
    if recheck_pain or recheck_all:
        typer.echo(f"  {pipeline.mark_pain_for_recheck(recheck_all)} posts sent back for re-checking")
    st = pipeline.classify_pending(limit=limit or None, log=typer.echo)
    typer.echo(f"  classified: {st.classified}  payment pain: {st.pain}  failed: {st.failed}  skipped: {st.skipped}")
    if st.stopped:
        typer.secho(f"  {st.stopped}", fg="yellow")


@cli.command()
def signals(limit: int = typer.Option(20, help="How many to show"),
            only_pain: bool = typer.Option(False, help="Only posts classified as payment pain"),
            export: Path = typer.Option(OUTPUT_DIR / "signals_review.csv", help="CSV for manual checking"),
            sample: bool = typer.Option(False, help="Random mix of LLM-classified posts, half pain and half not")) -> None:
    """Show stored signals with their classification, and export a CSV for manual review."""
    import random
    with db.session() as s:
        q = select(db.Signal).order_by(db.Signal.created_at.desc())
        rows = [r for r in s.exec(q).all() if not only_pain or r.is_payment_pain]
        if sample:
            rng = random.Random(7)
            done = [r for r in rows if r.status == "classified"]
            pain = [r for r in done if r.is_payment_pain]
            other = [r for r in done if not r.is_payment_pain]
            half = limit // 2
            rows = rng.sample(pain, min(half, len(pain))) + rng.sample(other, min(limit - half, len(other)))
            rng.shuffle(rows)
        rows = rows[:limit]
    export.parent.mkdir(parents=True, exist_ok=True)
    with open(export, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "source", "created", "title", "url", "is_payment_pain", "pain_type", "urgency",
                    "product_url", "reason", "your_verdict (right/wrong)"])
        for r in rows:
            c = r.classification or {}
            w.writerow([r.id, r.source, r.created_at.date(), r.title, r.url, r.is_payment_pain, r.pain_type,
                        r.urgency, r.product_url, c.get("one_line_reason", r.status), ""])
            mark = "skip" if r.status == "skipped" else {True: "PAIN", False: "no  ", None: "??  "}[r.is_payment_pain]
            typer.echo(f"[{mark}] #{r.id} {r.source:6} {(r.title or '')[:70]}")
            if c:
                typer.echo(f"         {r.pain_type}/{r.urgency} {r.product_url or ''} :: {c.get('one_line_reason', '')[:110]}")
            typer.echo(f"         {r.url}")
    typer.secho(f"\nExported {len(rows)} rows to {export}", fg="green")


if __name__ == "__main__":
    cli()
