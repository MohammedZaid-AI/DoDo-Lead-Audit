"""Finds currencies, price samples, tax hints and local payment methods in visible text."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from app.detect.textutil import is_negated, sentences

CURRENCY_ALIASES = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR",
    "$": "USD", "us$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
}

# Currency markers that count on their own (codes as whole words, ₹ € £ anywhere).
# "$" and "Rs" only count when followed by a number, to avoid code and prose.
_CODE_RE = re.compile(r"(?<![A-Za-z])(INR|USD|EUR|GBP)(?![A-Za-z])")
_SYMBOL_RE = re.compile(r"[₹€£]")
_DOLLAR_RE = re.compile(r"(?<![A-Za-z$])(?:US)?\$\s?\d")
_RS_RE = re.compile(r"(?<![A-Za-z])Rs\.?\s?\d")

_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_CUR = r"₹|US\$|\$|€|£|Rs\.?|INR|USD|EUR|GBP"
_PERIOD = (r"(?:(?:\s*/\s*|\s+(?:per|a|an)\s+)"
           r"(?P<period>(?:\d+[kKmM]?\s+)?[A-Za-z][A-Za-z\- ]{0,24}?)(?=[\s.,;:)!]|$))?")
PRICE_RE = re.compile(
    rf"(?P<cur>{_CUR})\s?(?P<amt>{_NUM})(?P<suffix>[kK])?"
    rf"(?:\s?(?:-|–|to)\s?(?:{_CUR})?\s?(?P<amt2>{_NUM}))?"
    rf"(?:\s*(?P<cur_after>INR|USD|EUR|GBP)(?![A-Za-z]))?"
    rf"{_PERIOD}"
)

PERIOD_MAP = {
    "month": "month", "mo": "month", "mon": "month", "monthly": "month",
    "year": "year", "yr": "year", "annum": "year", "annual": "year", "yearly": "year",
    "minute": "minute", "min": "minute", "mins": "minute",
    "hour": "hour", "hr": "hour", "hrs": "hour",
    "week": "week", "wk": "week", "day": "day",
    "user": "user", "seat": "seat", "call": "call", "request": "request",
}

_TAX_TAIL = (r"(?:\.|udes?|uded|uding|usive)?\s+(?:of\s+)?(?:all\s+)?(?:applicable\s+)?"
             r"(?:tax|taxes|VAT|GST)\b")
TAX_PATTERNS = {
    "VAT": r"\bVAT\b",
    "GST": r"\bGST\b",
    "sales tax": r"\bsales tax",
    "excl. tax": r"\bexcl" + _TAX_TAIL,
    "plus taxes": r"(?:\bplus|\+)\s*(?:applicable\s+)?(?:tax|taxes|GST|VAT)\b",
    "inclusive of taxes": r"\bincl" + _TAX_TAIL,
    "taxes shown at checkout": r"\btax(?:es)?\b[^.\n]{0,40}\bat checkout\b",
}

LOCAL_METHOD_PATTERNS = {
    "UPI": r"\bUPI\b",
    "PhonePe": r"\bPhone\s?Pe\b",
    "Google Pay": r"\bGoogle\s?Pay\b|\bGPay\b",
    "Paytm": r"\bPaytm\b",
    "netbanking": r"\bnet\s?-?banking\b",
    "wallets": (r"\b(?:mobile|digital|e-?|UPI|payment)\s*wallets?\b"
                r"|(?:UPI|cards?|net\s?banking)\W[^.\n]{0,30}\bwallets\b"
                r"|\bwallets?\b(?=[^.\n]{0,40}(?:Paytm|PhonePe|Amazon Pay|Mobikwik))"),
    "Pix": r"\bPix\b",
    "iDEAL": r"\biDEAL\b",
    "SEPA": r"\bSEPA\b",
}
_CASE_SENSITIVE = {"Pix", "iDEAL", "UPI", "SEPA", "VAT", "GST"}


@dataclass
class PriceSample:
    amount: float
    currency: str
    period: str | None
    raw_text: str
    amount_max: float | None = None


@dataclass
class PricingResult:
    currencies_seen: list[str] = field(default_factory=list)
    currency_mode: str = "unclear"
    has_currency_switcher: bool = False
    price_samples: list[PriceSample] = field(default_factory=list)
    tax_hints: list[str] = field(default_factory=list)
    local_methods: list[str] = field(default_factory=list)
    local_method_evidence: dict[str, str] = field(default_factory=dict)  # name -> sentence

    def to_dict(self) -> dict:
        return asdict(self)


def _norm_currency(token: str) -> str:
    return CURRENCY_ALIASES.get(token.lower(), token.upper())


def find_currencies(text: str) -> list[str]:
    """Currencies mentioned in text, ordered by first appearance."""
    hits: list[tuple[int, str]] = []
    for rx in (_CODE_RE, _SYMBOL_RE):
        hits += [(m.start(), _norm_currency(m.group(0))) for m in rx.finditer(text)]
    hits += [(m.start(), "USD") for m in _DOLLAR_RE.finditer(text)]
    hits += [(m.start(), "INR") for m in _RS_RE.finditer(text)]
    seen: list[str] = []
    for _, cur in sorted(hits):
        if cur not in seen:
            seen.append(cur)
    return seen


def find_switcher_currencies(controls: list[str]) -> list[str]:
    """Currencies offered as short control labels (buttons, tabs, options)."""
    out: list[str] = []
    for label in controls:
        t = label.strip()
        if re.fullmatch(r"(?:[₹$€£]\s?)?(?:INR|USD|EUR|GBP)(?:\s?\(?[₹$€£]\)?)?|[₹$€£]", t, re.I):
            cur = find_currencies(t) or [_norm_currency(t)]
            for c in cur:
                if c not in out:
                    out.append(c)
    return out


def _normalize_period(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().lower()
    first = raw.split()[0] if raw.split() else raw
    if re.fullmatch(r"\d+[km]?", first) and len(raw.split()) > 1:
        return raw  # e.g. "10k characters", "1m tokens"
    return PERIOD_MAP.get(first.rstrip("s") if first not in PERIOD_MAP else first, raw)


def _to_float(num: str) -> float:
    return float(num.replace(",", ""))


def find_prices(text: str, limit: int = 20) -> list[PriceSample]:
    """Price-like strings such as '₹9,999 / month', '$19/mo', '$0.09 - $0.21/minute'."""
    out: list[PriceSample] = []
    seen: set[str] = set()
    for line in text.splitlines():
        for m in PRICE_RE.finditer(line):
            raw = " ".join(m.group(0).split()).rstrip(" /")
            if raw in seen:
                continue
            seen.add(raw)
            amt = _to_float(m.group("amt")) * (1000 if m.group("suffix") else 1)
            amt2 = _to_float(m.group("amt2")) if m.group("amt2") else None
            out.append(PriceSample(amt, _norm_currency(m.group("cur")),
                                   _normalize_period(m.group("period")), raw, amt2))
            if len(out) >= limit:
                return out
    return out


def _find_named(text: str, patterns: dict[str, str]) -> list[str]:
    found = []
    for name, rx in patterns.items():
        flags = 0 if name in _CASE_SENSITIVE else re.I
        if re.search(rx, text, flags):
            found.append(name)
    return found


def find_local_methods(text: str) -> dict[str, str]:
    """Local payment methods named in a non-negated sentence. Returns name -> sentence."""
    sents = sentences(text)
    found: dict[str, str] = {}
    for name, rx in LOCAL_METHOD_PATTERNS.items():
        flags = 0 if name in _CASE_SENSITIVE else re.I
        for i, s in enumerate(sents):
            if re.search(rx, s, flags) and not is_negated(sents, i):
                found[name] = s[:200]
                break
    return found


def currency_mode(currencies: list[str], switcher: bool) -> str:
    if len(currencies) >= 2 or switcher:
        return "multi currency"
    if currencies == ["INR"]:
        return "INR only"
    if currencies == ["USD"]:
        return "USD only"
    return "unclear"


def analyze_pricing(text: str, controls: list[str] | None = None) -> PricingResult:
    """Run every pricing extractor on visible text plus control labels."""
    text_cur = find_currencies(text)
    switch_cur = find_switcher_currencies(controls or [])
    currencies = text_cur + [c for c in switch_cur if c not in text_cur]
    switcher = len(switch_cur) >= 2
    methods = find_local_methods(text)
    return PricingResult(
        currencies_seen=currencies,
        currency_mode=currency_mode(currencies, switcher),
        has_currency_switcher=switcher,
        price_samples=find_prices(text),
        tax_hints=_find_named(text, TAX_PATTERNS),
        local_methods=list(methods),
        local_method_evidence=methods,
    )
