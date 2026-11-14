"""Dashboard: leads sorted by score, filters, audit pages, and a 'Mark reviewed' checkbox.

Read only towards the outside world: it only reads the local database and the saved audit files.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlmodel import select

from app import db
from app.audit.render import TEMPLATES

app = FastAPI(title="Dodo Lead Audit", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(TEMPLATES))

PAIN_LABELS = {"stripe_access": "Stripe access", "mor_switch": "MoR switch", "tax_confusion": "Tax confusion",
               "choosing_gateway": "Choosing a gateway", "payout_issue": "Payouts", "other": "Other"}
SOURCE_LABELS = {"hn": "Hacker News", "github": "GitHub"}


def lead_rows() -> list[dict]:
    """One row per audit, joined with its company and source post."""
    with db.session() as s:
        rows = s.exec(select(db.Audit, db.Company).join(db.Company)).all()
        signals = {sig.id: sig for sig in s.exec(select(db.Signal).where(
            db.Signal.id.in_([a.signal_id for a, _ in rows if a.signal_id]))).all()}
    out = []
    for audit, company in rows:
        data = audit.data or {}
        setup = data.get("setup", {})
        sig = signals.get(audit.signal_id)
        out.append({
            "id": audit.id, "score": audit.score, "company": data.get("company_name") or company.name,
            "domain": company.domain, "url": company.url,
            "provider": setup.get("provider_label") or "Not verified", "mor": setup.get("has_mor", "not verified"),
            "currency_mode": setup.get("currency_mode", "unclear"),
            "pain_type": sig.pain_type if sig else None,
            "pain_label": PAIN_LABELS.get(sig.pain_type, sig.pain_type) if sig else "",
            "urgency": sig.urgency if sig else None,
            "source": SOURCE_LABELS.get(sig.source, sig.source) if sig else "Manual audit",
            "source_url": sig.url if sig else None, "post_title": sig.title if sig else "",
            "reasons": data.get("score_reasons", []), "has_opener": bool(audit.opener),
            "reviewed": audit.reviewed,
        })
    out.sort(key=lambda r: (-(r["score"] if r["score"] is not None else -1), r["company"].lower()))
    return out


def apply_filters(rows: list[dict], provider: str = "", pain_type: str = "", min_score: float | None = None,
                  reviewed: str = "") -> list[dict]:
    if provider:
        rows = [r for r in rows if r["provider"] == provider]
    if pain_type:
        rows = [r for r in rows if r["pain_type"] == pain_type]
    if min_score is not None:
        rows = [r for r in rows if (r["score"] or 0) >= min_score]
    if reviewed in ("yes", "no"):
        rows = [r for r in rows if r["reviewed"] == (reviewed == "yes")]
    return rows


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, provider: str = "", pain_type: str = "", min_score: str = "", reviewed: str = ""):
    every = lead_rows()
    try:
        min_val = float(min_score) if min_score.strip() else None
    except ValueError:
        min_val = None
    rows = apply_filters(every, provider, pain_type, min_val, reviewed)
    return templates.TemplateResponse(request, "dashboard.html", {
        "rows": rows, "total": len(every), "reviewed_count": sum(r["reviewed"] for r in every),
        "providers": sorted({r["provider"] for r in every}),
        "pain_types": sorted({r["pain_type"] for r in every if r["pain_type"]}), "pain_labels": PAIN_LABELS,
        "f": {"provider": provider, "pain_type": pain_type, "min_score": min_score, "reviewed": reviewed},
    })


BACK_BAR = ('<div style="max-width:720px;margin:0 auto;padding:14px 20px 0;font:14px system-ui,sans-serif">'
            '<a href="/" style="color:#2f5d50">&larr; All leads</a></div>')


@app.get("/audit/{audit_id}", response_class=HTMLResponse)
def audit_page(audit_id: int):
    """The same standalone file saved in output/audits/, with a link back to the list."""
    with db.session() as s:
        audit = s.get(db.Audit, audit_id)
    if audit is None or not audit.html_path or not Path(audit.html_path).exists():
        raise HTTPException(404, "Audit not found. Run 'python cli.py run' to create audits.")
    html = Path(audit.html_path).read_text(encoding="utf-8")
    return html.replace("<body>", "<body>" + BACK_BAR, 1)


class ReviewedIn(BaseModel):
    reviewed: bool


@app.post("/audit/{audit_id}/reviewed")
def set_reviewed(audit_id: int, body: ReviewedIn):
    with db.session() as s:
        audit = s.get(db.Audit, audit_id)
        if audit is None:
            raise HTTPException(404, "Audit not found")
        audit.reviewed = body.reviewed
        s.add(audit)
        s.commit()
        return {"id": audit_id, "reviewed": audit.reviewed}
