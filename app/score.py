"""Lead score 0 to 10 (brief section 13). Every point comes with a plain reason."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

NON_MOR_PROVIDERS = {"stripe", "razorpay", "cashfree", "payu", "juspay", "paypal"}
MOR_SWITCH_PROVIDERS = {"lemonsqueezy", "paddle"}
FOREIGN_CURRENCIES = {"USD", "EUR", "GBP"}
GLOBAL_RE = re.compile(r"\b(?:international\w*|global(?:ly)?|abroad|overseas|worldwide|outside (?:of )?india|"
                       r"foreign (?:customers|buyers|users)|usd|us customers|eu customers)\b", re.I)
DODO_RE = re.compile(r"\bdodo\s?payments\b|\bdodopayments\b", re.I)


@dataclass
class LeadFacts:
    """What the score needs, from the signal and the detection. Plain values so it is easy to test."""
    is_payment_pain: bool = False
    urgency: str | None = None
    sells_digital_product: bool = False
    signal_text: str = ""
    posted_at: datetime | None = None
    provider: str | None = None          # detected primary provider
    providers: list[str] = field(default_factory=list)  # every provider seen with medium or high confidence
    has_mor: bool | None = None
    currency_mode: str = "unclear"
    currencies: list[str] = field(default_factory=list)
    confidence: str = "low"


@dataclass
class Score:
    value: float
    reasons: list[str]

    def text(self) -> str:
        return "; ".join(self.reasons)


def score_lead(f: LeadFacts, now: datetime | None = None) -> Score:
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    pts = 0.0

    def add(n: float, why: str) -> None:
        nonlocal pts
        pts += n
        reasons.append(f"{n:+g} {why}")

    if f.provider == "dodo" or "dodo" in f.providers:
        return Score(0, ["0: already on Dodo Payments"])
    if DODO_RE.search(f.signal_text):
        return Score(0, ["0: their post says they use or are setting up Dodo Payments (check by hand)"])

    if f.is_payment_pain:
        add(3, "real payment pain in their post")
    if f.urgency == "high":
        add(2, "urgency high")
    elif f.urgency == "medium":
        add(1, "urgency medium")
    foreign = sorted(set(f.currencies) & FOREIGN_CURRENCIES)
    if foreign:
        add(2, f"prices in {', '.join(foreign)}")
    if f.provider in NON_MOR_PROVIDERS and f.has_mor is not True:
        add(2, f"on {f.provider} with no merchant of record")
    elif f.provider in MOR_SWITCH_PROVIDERS:
        add(2, f"on {f.provider}")
    if f.currency_mode == "INR only" and GLOBAL_RE.search(f.signal_text):
        add(1, "INR only pricing but the post talks about global buyers")
    if f.posted_at:
        posted = f.posted_at if f.posted_at.tzinfo else f.posted_at.replace(tzinfo=timezone.utc)
        if now - posted <= timedelta(days=3):
            add(1, "posted in the last 3 days")
    if f.confidence == "low":
        add(-2, "provider detection confidence is low")
    return Score(max(0.0, min(10.0, pts)), reasons)


def facts_for(signal, detection) -> LeadFacts:
    """Build LeadFacts from a db.Signal (or None) and a DetectionResult (or None)."""
    f = LeadFacts()
    if signal is not None:
        c = signal.classification or {}
        f.is_payment_pain = bool(signal.is_payment_pain)
        f.urgency = signal.urgency
        f.sells_digital_product = bool(c.get("sells_digital_product"))
        f.signal_text = f"{signal.title or ''}\n{signal.body or ''}"
        f.posted_at = signal.created_at
    if detection is not None:
        p, pr = detection.provider, detection.pricing
        f.provider, f.has_mor, f.confidence = p.primary, p.has_mor, p.confidence
        f.providers = [n for n, conf in p.providers.items() if conf in ("medium", "high")]
        f.currency_mode, f.currencies = pr.currency_mode, list(pr.currencies_seen)
    return f
