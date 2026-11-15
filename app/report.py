"""One self-contained HTML file with the ranked leads and every audit, to send to someone who will not install
the tool. Built from the database, so it reflects the last run (and any redrafted openers)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import select

from app import db
from app.audit.render import _env
from app.settings import OUTPUT_DIR

PAIN_LABELS = {"stripe_access": "Stripe access", "mor_switch": "MoR switch", "tax_confusion": "Tax confusion",
               "choosing_gateway": "Choosing a gateway", "payout_issue": "Payouts", "other": "Other"}
SOURCE_LABELS = {"hn": "Hacker News", "github": "GitHub"}
POST_SOURCES = tuple(SOURCE_LABELS)


def funnel(s) -> dict:
    """How many public posts went through each step, across all runs in the database."""
    posts = [p for p in s.exec(select(db.Signal)).all() if p.source in POST_SOURCES]
    dates = [p.created_at for p in posts]
    return {
        "read": len(posts),
        "prefiltered": sum(p.status == "skipped" for p in posts),
        "classified": sum(p.status == "classified" for p in posts),
        "pain": sum(bool(p.is_payment_pain) for p in posts),
        "from": min(dates).date().isoformat() if dates else "",
        "to": max(dates).date().isoformat() if dates else "",
    }


def report_leads(s) -> list[dict]:
    rows = s.exec(select(db.Audit, db.Company).join(db.Company).where(db.Audit.signal_id != None)).all()  # noqa: E711
    leads = []
    for audit, company in rows:
        data = dict(audit.data or {})
        sig = s.get(db.Signal, audit.signal_id)
        setup = data.get("setup", {})
        leads.append({
            "a": data, "anchor": re.sub(r"[^a-z0-9]+", "-", company.domain.lower()).strip("-"),
            "score": audit.score or 0, "reasons": data.get("score_reasons", []),
            "company": data.get("company_name") or company.name, "domain": company.domain,
            "provider": setup.get("provider_label") or "Not verified", "mor": setup.get("has_mor", "not verified"),
            "currency": setup.get("currency_mode", "unclear"),
            "pain": PAIN_LABELS.get(sig.pain_type, sig.pain_type or "") if sig else "",
            "urgency": sig.urgency if sig else "", "source": SOURCE_LABELS.get(sig.source, sig.source) if sig else "",
            "post_url": sig.url if sig else "", "has_opener": bool(audit.opener),
        })
    leads.sort(key=lambda l: (-l["score"], l["company"].lower()))
    return leads


def build_report(path: Path = OUTPUT_DIR / "report.html", author: str = "", repo_url: str = "") -> Path:
    with db.session() as s:
        leads = report_leads(s)
        stats = funnel(s)
    stats["audited"] = len(leads)
    stats["openers"] = sum(l["has_opener"] for l in leads)
    html = _env.get_template("report.html").render(
        leads=leads, stats=stats, author=author, repo_url=repo_url,
        providers=sorted({l["provider"] for l in leads}), pains=sorted({l["pain"] for l in leads if l["pain"]}),
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
