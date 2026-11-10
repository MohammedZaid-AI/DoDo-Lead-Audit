"""Command line entry point for Dodo Lead Audit."""
from __future__ import annotations

import json
import re
from pathlib import Path

import typer

from app.detect import DetectionResult, detect
from app.detect.browser import Browser
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


def _run_urls(urls: list[str], save_fixtures: bool) -> list[DetectionResult]:
    results = []
    out = OUTPUT_DIR / "detections"
    out.mkdir(parents=True, exist_ok=True)
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
    return results


@cli.command()
def audit(url: str, save_fixtures: bool = typer.Option(False, help="Save rendered pages to tests/fixtures")) -> None:
    """Run detection on one url and print a summary."""
    _run_urls([url], save_fixtures)


@cli.command("audit-file")
def audit_file(path: Path, save_fixtures: bool = typer.Option(False, help="Save rendered pages to tests/fixtures")) -> None:
    """Run detection on every url in a text file (one per line)."""
    urls = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    _run_urls(urls, save_fixtures)


if __name__ == "__main__":
    cli()
