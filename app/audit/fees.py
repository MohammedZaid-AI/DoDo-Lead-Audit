"""Fee comparison math.

Picks the sale amount (first clear monthly price, INR converted at INR_PER_USD and
labelled approximate, else a labelled example amount), then computes the fee per
sale for a US buyer and a non-US buyer for: the current provider, a Paddle or
Lemon Squeezy reference line, and Dodo. Never claims Dodo is cheaper than Stripe or
Razorpay: for non-MoR providers the comparison is written as text about tax work.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.detect.pricing import PriceSample
from app.settings import inr_per_usd, load_yaml

PROVIDER_RATE_KEY = {
    "stripe": "stripe", "paddle": "paddle", "lemonsqueezy": "lemonsqueezy",
    "razorpay": "razorpay_intl_cards", "dodo": "dodo",
}
LABELS = {"stripe": "Stripe", "paddle": "Paddle", "lemonsqueezy": "Lemon Squeezy",
          "razorpay_intl_cards": "Razorpay (international cards)", "dodo": "Dodo Payments"}
MOR_RATE_KEYS = {"paddle", "lemonsqueezy", "dodo"}


@dataclass
class SaleBasis:
    amount_usd: float
    label: str
    is_example: bool
    is_subscription: bool
    raw_text: str | None = None


@dataclass
class FeeRow:
    rate_key: str | None
    label: str
    role: str  # current | reference | dodo
    is_mor: bool | None
    rate_text: str
    us_fee: float | None
    intl_fee: float | None
    notes: list[str] = field(default_factory=list)


@dataclass
class FeeTable:
    basis: SaleBasis
    rows: list[FeeRow]
    comparison: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def fee(amount: float, percent: float, fixed: float) -> float:
    """Fee on one sale, rounded half up to the cent."""
    value = Decimal(str(amount)) * Decimal(str(percent)) / Decimal(100) + Decimal(str(fixed))
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def effective_percent(rate: dict, international: bool, subscription: bool) -> float:
    pct = float(rate["percent"])
    if international:
        pct += float(rate.get("international_pct", 0) or 0)
    if subscription:
        pct += float(rate.get("subscription_pct", 0) or 0)
    return pct


def fee_for(rate: dict, amount: float, international: bool, subscription: bool) -> float:
    return fee(amount, effective_percent(rate, international, subscription), float(rate.get("fixed_usd", 0)))


def _money(x: float) -> str:
    return f"${x:,.2f}"


def pick_sale_basis(samples: list[PriceSample], rate_inr: float | None = None,
                    reference_usd: float | None = None) -> SaleBasis:
    """First clear monthly price above zero; INR converted and labelled approximate."""
    rate_inr = rate_inr or inr_per_usd()
    for s in samples:
        if s.period == "month" and s.amount > 0 and s.amount_max is None and s.currency in ("USD", "INR"):
            if s.currency == "USD":
                return SaleBasis(s.amount, f"{s.raw_text} (first monthly price on the page).", False, True, s.raw_text)
            usd = round(s.amount / rate_inr, 2)
            return SaleBasis(usd, f"{s.raw_text} is about {_money(usd)} at ₹{rate_inr:g} per USD (approximate).",
                             False, True, s.raw_text)
    ref = reference_usd or float(load_yaml("fees").get("defaults", {}).get("reference_sale_usd", 20))
    subscription = any(s.period in ("month", "year") for s in samples)
    kind = "subscription" if subscription else "one-time"
    return SaleBasis(ref, f"Example {_money(ref)} {kind} sale. No clear monthly price was found on the page.",
                     True, subscription)


def _rate_text(rate: dict, subscription: bool) -> str:
    base = f"{rate['percent']:g}% + {_money(float(rate.get('fixed_usd', 0)))}"
    extras = []
    if rate.get("international_pct"):
        extras.append(f"+{rate['international_pct']:g}% non-US")
    if subscription and rate.get("subscription_pct"):
        extras.append(f"+{rate['subscription_pct']:g}% subscriptions")
    return base + (f" ({', '.join(extras)})" if extras else "")


def _row(key: str, role: str, basis: SaleBasis, rates: dict) -> FeeRow:
    rate = rates[key]
    notes = [n for n in (rate.get("intl_note"), rate.get("note")) if n]
    return FeeRow(key, LABELS.get(key, key), role, key in MOR_RATE_KEYS, _rate_text(rate, basis.is_subscription),
                  fee_for(rate, basis.amount_usd, False, basis.is_subscription),
                  fee_for(rate, basis.amount_usd, True, basis.is_subscription), notes)


def build_fee_table(provider: str | None, provider_label: str | None, samples: list[PriceSample],
                    rates: dict | None = None, rate_inr: float | None = None) -> FeeTable:
    """Fee rows plus plain-English comparison lines for the audit."""
    rates = rates or load_yaml("fees")
    basis = pick_sale_basis(samples, rate_inr, rates.get("defaults", {}).get("reference_sale_usd"))
    key = PROVIDER_RATE_KEY.get(provider or "")
    rows: list[FeeRow] = []
    if provider and provider != "dodo":
        if key and key in rates:
            rows.append(_row(key, "current", basis, rates))
        else:
            rows.append(FeeRow(None, provider_label or provider, "current", None, "rate not on file", None, None,
                               ["Fee rate for this provider is not on file, so no number is shown."]))
    if key not in ("paddle", "lemonsqueezy"):
        rows.append(_row("paddle", "reference", basis, rates))
    rows.append(_row("dodo", "dodo", basis, rates))
    return FeeTable(basis, rows, _comparison(provider, provider_label, key, rows, basis))


def _comparison(provider: str | None, label: str | None, key: str | None, rows: list[FeeRow],
                basis: SaleBasis) -> list[str]:
    dodo = next(r for r in rows if r.role == "dodo")
    lines: list[str] = []
    if provider is None:
        lines.append("The current payment provider could not be verified from public pages, "
                     "so no current fee is shown.")
    elif provider == "dodo":
        lines.append("Already on Dodo Payments.")
    elif key in ("paddle", "lemonsqueezy"):
        cur = rows[0]
        lines.append(_mor_line(cur, dodo, basis))
    else:
        name = label or provider
        lines.append(f"{name} charges less per sale, but they stay the seller of record. They register, file and "
                     f"pay tax themselves in every country where they cross a threshold. Dodo's fee includes "
                     f"acting as the Merchant of Record, which takes on that tax work.")
    src = load_yaml("fees")["dodo"].get("source", "dodopayments.com/pricing")
    lines.append(f"Dodo rate used: {dodo.rate_text}, from {src}.")
    return lines


def _mor_line(cur: FeeRow, dodo: FeeRow, basis: SaleBasis) -> str:
    """Compare two MoRs per buyer region, stating only what the numbers show."""
    parts = []
    for region, c, d in (("a US buyer", cur.us_fee, dodo.us_fee), ("a non-US buyer", cur.intl_fee, dodo.intl_fee)):
        if c is None or d is None:
            continue
        if d < c:
            parts.append(f"for {region} Dodo takes {_money(d)} vs {_money(c)} ({_money(c - d)} less)")
        elif d > c:
            parts.append(f"for {region} Dodo takes {_money(d)} vs {_money(c)} ({_money(d - c)} more)")
        else:
            parts.append(f"for {region} both take {_money(d)}")
    return f"Both are Merchants of Record. On a {_money(basis.amount_usd)} sale, " + "; ".join(parts) + "."
