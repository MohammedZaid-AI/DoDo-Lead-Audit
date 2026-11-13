"""SQLModel tables (Company, Detection, Signal, Audit) and session helpers. SQLite file: output/leads.db."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, Session, SQLModel, create_engine, select

from app.settings import OUTPUT_DIR


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Company(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    url: str
    domain: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=_now)


class Detection(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    pages_checked: list = Field(default_factory=list, sa_column=Column(JSON))
    provider: str | None = None
    provider_evidence: list = Field(default_factory=list, sa_column=Column(JSON))
    has_mor: bool | None = None
    currencies_seen: list = Field(default_factory=list, sa_column=Column(JSON))
    currency_mode: str = "unclear"
    price_samples: list = Field(default_factory=list, sa_column=Column(JSON))
    tax_hints: list = Field(default_factory=list, sa_column=Column(JSON))
    local_methods: list = Field(default_factory=list, sa_column=Column(JSON))
    confidence: str = "low"
    checked_at: datetime = Field(default_factory=_now)


class Signal(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source: str  # hn | github
    url: str = Field(index=True, unique=True)
    author: str | None = None
    title: str | None = None
    body: str | None = None
    created_at: datetime = Field(default_factory=_now)
    pain_type: str | None = None
    urgency: str | None = None
    product_url: str | None = None
    company_id: int | None = Field(default=None, foreign_key="company.id")
    classification: dict | None = Field(default=None, sa_column=Column(JSON))
    context: dict = Field(default_factory=dict, sa_column=Column(JSON))  # phrase, repo, story url
    is_payment_pain: bool | None = None
    status: str = "pending"  # pending | classified | failed
    fetched_at: datetime = Field(default_factory=_now)


class Audit(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    signal_id: int | None = Field(default=None, foreign_key="signal.id")
    summary: str = ""
    issues: list = Field(default_factory=list, sa_column=Column(JSON))
    fee_table: dict = Field(default_factory=dict, sa_column=Column(JSON))
    switch_effort: dict = Field(default_factory=dict, sa_column=Column(JSON))
    opener: str = ""
    score: float | None = None
    html_path: str | None = None
    data: dict = Field(default_factory=dict, sa_column=Column(JSON))  # full audit for re-rendering
    reviewed: bool = False
    created_at: datetime = Field(default_factory=_now)


_engine = None


def engine():
    global _engine
    if _engine is None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{OUTPUT_DIR / 'leads.db'}")
        SQLModel.metadata.create_all(_engine)
    return _engine


def session() -> Session:
    return Session(engine())


def upsert_company(s: Session, name: str, url: str, domain: str) -> Company:
    c = s.exec(select(Company).where(Company.domain == domain)).first()
    if c is None:
        c = Company(name=name, url=url, domain=domain)
        s.add(c)
        s.commit()
        s.refresh(c)
    return c


def save_detection(s: Session, company_id: int, d: Any) -> Detection:
    """Store a DetectionResult (app.detect)."""
    row = Detection(
        company_id=company_id, pages_checked=d.pages_checked, provider=d.provider.primary,
        provider_evidence=[e.__dict__ for e in d.provider.evidence], has_mor=d.provider.has_mor,
        currencies_seen=d.pricing.currencies_seen, currency_mode=d.pricing.currency_mode,
        price_samples=[p.__dict__ for p in d.pricing.price_samples], tax_hints=d.pricing.tax_hints,
        local_methods=d.pricing.local_methods, confidence=d.provider.confidence,
    )
    s.add(row)
    s.commit()
    return row


def save_audit(s: Session, company_id: int, audit: Any, html_path: str,
               signal_id: int | None = None, score: float | None = None) -> Audit:
    """Store an audit (app.audit.builder.Audit)."""
    data = audit.to_dict()
    row = Audit(company_id=company_id, signal_id=signal_id, summary=audit.summary, issues=data["issues"],
                fee_table=data["fee_table"], switch_effort=audit.switch_effort, opener=audit.opener,
                score=score, html_path=html_path, data=data)
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def store_signals(s: Session, raws: list[Any]) -> int:
    """Insert RawSignals (app.listen) not already stored, deduplicated by url. Returns new count."""
    known = set(s.exec(select(Signal.url)).all())
    new = 0
    for r in raws:
        if r.url in known:
            continue
        known.add(r.url)
        s.add(Signal(source=r.source, url=r.url, author=r.author, title=r.title, body=r.body,
                     created_at=r.created_at, context=r.context))
        new += 1
    s.commit()
    return new
