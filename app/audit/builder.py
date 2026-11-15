"""Turns a DetectionResult (and optional source signal) into an Audit.

Every issue is only added when the detection data backs it, and carries the
evidence that triggered it. Unknowns are written as "not verified".
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.audit.fees import FeeTable, build_fee_table
from app.audit.opener import build_facts, check_opener, draft_opener
from app.detect import DetectionResult
from app.detect.providers import fingerprints

INDIA_METHODS = {"UPI", "PhonePe", "Paytm", "netbanking", "Google Pay"}
MIGRATABLE = {"stripe", "paddle", "lemonsqueezy", "polar"}
TITLE_SPLIT = re.compile(r"\s+[|–—\-:·•]\s+|\s*\|\s*|:\s+")


@dataclass
class Issue:
    key: str
    title: str
    detail: str
    evidence: str


@dataclass
class SetupInfo:
    provider: str | None
    provider_label: str
    has_mor: str  # yes | no | not verified | yes (verify)
    currencies: list[str]
    currency_mode: str
    price_sample: str
    price_context: str  # page line the sample came from; empty when the unit is unclear
    confidence: str
    tax_hints: list[str]
    local_methods: list[str]


@dataclass
class Audit:
    company_name: str
    url: str
    domain: str
    what_they_sell: str
    summary: str
    setup: SetupInfo
    issues: list[Issue]
    fee_table: FeeTable
    switch_effort: dict
    opener: str
    signal: dict | None
    evidence: list[dict]
    pages_checked: list[str]
    page_errors: dict[str, str]
    checked_at: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    opener_note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def provider_label(provider: str | None) -> str:
    if not provider:
        return "Not verified"
    names = fingerprints().get(provider, {}).get("names") or [provider]
    return names[0]


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


SHARED_HOSTS = ("vercel.app", "netlify.app", "github.io", "pages.dev", "onrender.com", "herokuapp.com",
                "fly.dev", "web.app", "firebaseapp.com", "railway.app")
GENERIC_LABELS = {"www", "app", "docs", "doc", "portal", "admin", "dashboard", "my", "go", "get", "try", "api",
                  "shop", "store", "health", "web", "site", "home", "beta", "dev", "staging"}


def _brand_label(domain: str) -> str:
    """The part of the host that names the product: 'docs.agno.com' -> 'agno', 'lumina.vercel.app' -> 'lumina'."""
    parts = domain.split(".")
    if any(domain.endswith("." + h) for h in SHARED_HOSTS):
        return parts[0]
    labels = parts[:-1] if len(parts) > 1 else parts
    for label in labels:
        if label not in GENERIC_LABELS:
            return label
    return labels[0]


def company_name(title: str, domain: str) -> str:
    """Brand name from the page title, matched against the domain's product label."""
    label = _brand_label(domain)
    for candidate in dict.fromkeys([label, label.replace("-", " "), label.replace("-", "")]):
        m = re.search(rf"\b({re.escape(candidate)}(?:\.[a-z]{{2,4}})?)\b", title or "", re.I)
        if m:
            return m.group(1)
    if domain.endswith(".github.io") and title:
        first = re.split(r"\s+[|:–—-]\s+", title.strip())[0].strip()
        if 0 < len(first) <= 40:
            return first
    return " ".join(w.capitalize() for w in label.split("-"))


def what_they_sell(title: str, meta: str) -> str:
    text = (meta or "").strip() or (title or "").strip()
    if not text:
        return "Not verified (no page title or description found)."
    first = re.split(r"(?<=[.!?])\s", text)[0]
    return first if len(first) <= 200 else first[:197].rstrip() + "..."


def _mor_text(provider: str | None, has_mor: bool | None) -> str:
    if has_mor is None:
        return "not verified"
    if provider == "gumroad":
        return "yes (verify)"
    return "yes" if has_mor else "no"


def build_setup(d: DetectionResult) -> SetupInfo:
    p, pr = d.provider, d.pricing
    priced = [s for s in pr.price_samples if s.amount > 0]
    with_unit = next((s for s in priced if s.period), None)
    if with_unit:
        sample, context = with_unit.raw_text, with_unit.context
    else:
        sample = f"{priced[0].raw_text} (unit not clear on the page)" if priced else None
        context = ""
    return SetupInfo(
        provider=p.primary, provider_label=provider_label(p.primary), has_mor=_mor_text(p.primary, p.has_mor),
        currencies=pr.currencies_seen, currency_mode=pr.currency_mode,
        price_sample=sample or "not verified (no price found)", price_context=context, confidence=p.confidence,
        tax_hints=pr.tax_hints, local_methods=pr.local_methods,
    )


def _primary_evidence(d: DetectionResult) -> str:
    e = next((e for e in d.provider.evidence if e.provider == d.provider.primary), None)
    return f"'{e.matched[:140]}' ({e.location}, {e.page_url})" if e else ""


def find_issues(d: DetectionResult, fees: FeeTable, company: str = "The company") -> list[Issue]:
    """Issues backed by evidence, in the order a rep would raise them."""
    p, pr = d.provider, d.pricing
    issues: list[Issue] = []
    name = provider_label(p.primary)
    if pr.currency_mode == "INR only":
        samples = ", ".join(s.raw_text for s in pr.price_samples[:3])
        issues.append(Issue("inr_only", "Prices shown only in INR",
                            "Overseas buyers see no price in their own currency.",
                            f"Only INR found on the pages checked. Samples: {samples}"))
    india = [m for m in pr.local_methods if m in INDIA_METHODS]
    card_mentioned = any("card" in pr.local_method_evidence.get(m, "").lower() for m in india)
    if india and not card_mentioned:
        issues.append(Issue("india_methods", "Payment options named are India specific",
                            f"The page names {', '.join(india)} with no card option alongside, "
                            "which gives overseas buyers fewer ways to pay.",
                            "; ".join(f"'{pr.local_method_evidence[m][:120]}'" for m in india)))
    if p.has_mor is False:
        issues.append(Issue("not_mor", f"{name} is not a Merchant of Record",
                            f"{name} only processes the payment. {company} itself is the seller of record, so "
                            f"{company} owns tax registration, filing and compliance "
                            "in every market where they cross a threshold.", _primary_evidence(d)))
    if p.primary == "lemonsqueezy" and p.has_mor is not None:
        issues.append(Issue("ls_stripe", "Lemon Squeezy is moving into Stripe Managed Payments",
                            "Roadmap and support may change as the platform moves.",
                            "lemonsqueezy.com shows a '2026 Update: Lemon Squeezy + Stripe Managed Payments' banner "
                            "(checked 2026-11-11). " + _primary_evidence(d)))
    if p.primary in ("paddle", "lemonsqueezy") and p.has_mor is not None:
        cur, dodo = fees.rows[0], fees.rows[-1]
        if dodo.us_fee is not None and cur.us_fee is not None and dodo.us_fee < cur.us_fee:
            issues.append(Issue("mor_fee", f"Higher fee per sale on {name} for US buyers",
                                fees.comparison[0], f"Rates in config/fees.yaml; sale basis: {fees.basis.label}"))
    foreign = [c for c in pr.currencies_seen if c != "INR"]
    if foreign and not pr.tax_hints and p.has_mor is not True:
        issues.append(Issue("no_tax_line", "No tax line on pricing (worth checking)",
                            f"Prices are shown in {', '.join(foreign)} but no VAT, GST or sales tax line appears on "
                            "the pages checked. How tax is handled for overseas buyers is worth checking.",
                            f"No tax wording found on: {', '.join(d.pages_checked)}"))
    return issues


def switch_effort(provider: str | None) -> dict:
    label = provider_label(provider)
    if provider == "dodo":
        return {"kind": "none", "text": "Already on Dodo Payments.", "commands": []}
    if provider in MIGRATABLE:
        cmd = fingerprints()[provider].get("migrate_cmd")
        return {"kind": "one_command",
                "text": f"dodo-migrate (beta) moves products, customers and discount codes from {label} "
                        "into Dodo. Needs Node 18+ and API keys for both accounts.",
                "commands": ["npm install -g dodo-migrate", cmd]}
    if provider is None:
        return {"kind": "unknown",
                "text": "Provider not verified. If they are on Stripe, Paddle, Lemon Squeezy or Polar, "
                        "dodo-migrate can move products and customers in one command. Otherwise products and "
                        "customers need to be created in Dodo by hand.",
                "commands": []}
    return {"kind": "manual", "text": "Manual setup: products and customers need to be created in Dodo.",
            "commands": []}


def build_summary(name: str, s: SetupInfo, issues: list[Issue]) -> str:
    provider = s.provider_label if s.provider else "an unverified provider"
    mor = {"yes": "with a Merchant of Record", "no": "without a Merchant of Record",
           "yes (verify)": "likely with a Merchant of Record (verify)"}.get(s.has_mor, "MoR status not verified")
    mode = s.currency_mode if s.currency_mode != "unclear" else "currency unclear"
    found = f"{len(issues)} issue{'s' if len(issues) != 1 else ''} worth raising" if issues else "no issues backed by evidence"
    if not s.provider:
        return (f"{name}'s payment provider could not be verified from public pages. "
                f"Pricing: {mode}. {found.capitalize()}.")
    return f"{name} takes payments through {provider}, {mor}. Pricing: {mode}. {found.capitalize()}."


def build_audit(d: DetectionResult, signal: dict | None = None, with_opener: bool = True,
                keep_opener: str = "") -> Audit:
    """keep_opener: an opener drafted earlier for the same post. It is reused, without an LLM call,
    when it still passes every check against this audit's facts."""
    domain = domain_of(d.pages_checked[0] if d.pages_checked else d.url)
    name = company_name(d.title, domain)
    setup = build_setup(d)
    fees = build_fee_table(d.provider.primary, setup.provider_label, d.pricing.price_samples)
    issues = find_issues(d, fees, name)
    audit = Audit(
        company_name=name, url=d.url, domain=domain, what_they_sell=what_they_sell(d.title, d.meta_description),
        summary=build_summary(name, setup, issues), setup=setup, issues=issues, fee_table=fees,
        switch_effort=switch_effort(d.provider.primary), opener="", signal=signal,
        evidence=[asdict(e) for e in d.provider.evidence], pages_checked=d.pages_checked,
        page_errors=d.page_errors, checked_at=d.checked_at,
    )
    if with_opener and keep_opener and not check_opener(keep_opener, build_facts(audit.to_dict(), signal)):
        audit.opener, audit.opener_note = keep_opener, ""
    elif with_opener:
        result = draft_opener(audit.to_dict(), signal)
        audit.opener = result.text if result.ok else ""
        audit.opener_note = "" if result.ok else "Opener failed checks: " + "; ".join(result.problems)
    else:
        audit.opener_note = "Opener not drafted (skipped)."
    return audit
