"""LLM drafted opening message for a sales rep, checked against hard rules.

The model only sees facts taken from the audit. Its reply is then checked in code:
at most 60 words, no dashes, Dodo named at most once, no hype words, ends with a
question, and every number or provider name must appear in the facts. One retry
with the problems listed; after that the opener is marked failed, never used as is.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.detect.providers import fingerprints
from app.llm import LLMError, chat_json

MAX_WORDS = 60
HYPE_WORDS = ["revolutionary", "game changer", "game-changer", "seamless", "seamlessly", "effortless", "supercharge",
              "unlock", "cutting edge", "best in class", "world class", "amazing", "incredible", "skyrocket",
              "leverage", "synergy", "disrupt", "10x", "turbocharge", "hassle free", "magic"]
DASH_RE = re.compile(r"[-‐‑‒–—―]")
HARD_ASK_RE = re.compile(r"\b(?:quick chat|a chat|hop on|jump on|a call|book|schedule|demo|meeting|"
                         r"open to exploring|exploring how)\b", re.I)
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
PRICE_UNIT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:/|per|a|an)\s*([A-Za-z]{2,})", re.I)

SYSTEM = """You draft the first message a sales rep at Dodo Payments could send to a software founder.
Dodo Payments is a Merchant of Record for SaaS, AI and digital products: as the legal seller it handles
payments, global tax, invoices and compliance.

Rules:
1. At most 60 words.
2. Plain, friendly English. No hype words. No dashes or hyphens of any kind (write "pay as you go", not "pay-as-you-go").
3. Open with one specific, concrete detail from FACTS: their payment provider, a real price, their currency,
   or a line from their post. Use the company or product name. Do not start with "I noticed".
4. Mention Dodo at most once. Not mentioning it is fine.
5. End with one soft question about their situation, for example where most of their buyers are or how they
   handle tax today. Do not ask for a call, chat, demo or meeting. No links, no sign off.
6. Use only what is in FACTS. Do not state any number, provider, country or feature that is not in FACTS.
   Anything marked "not verified" must not be stated as fact.
7. Friendly, not an audit or an accusation. Tax points are "worth checking", never "you are non compliant".
8. If you quote a price, quote it exactly as in price_on_page, with what that line says it is for.
   Never attach a unit or product to a price that the line does not give it. Do not mix prices from different facts.
9. Get roles right. A payment processor such as Stripe or Razorpay is NOT the seller of record. When
   merchant_of_record is "no", the company itself is the seller of record and owns the tax work.

Return {"opener": "<message>"}. Respond only with JSON."""


class OpenerOut(BaseModel):
    opener: str


@dataclass
class OpenerResult:
    text: str
    ok: bool
    problems: list[str] = field(default_factory=list)


def build_facts(audit: dict, signal: dict | None = None) -> dict:
    """The only facts the model may use, taken from the audit."""
    s = audit["setup"]
    facts: dict[str, Any] = {
        "company": audit["company_name"],
        "what_they_sell": audit["what_they_sell"],
        "payment_provider": s["provider_label"] if s["provider"] else "not verified",
        "merchant_of_record": s["has_mor"],
        "pricing_currency": s["currency_mode"],
        # Only a price whose unit and purpose are clear, with the page line it came from.
        "price_on_page": s["price_context"] or "not verified",
        "tax_wording_on_pricing_page": s["tax_hints"] or "none found",
        "local_payment_methods_named": s["local_methods"] or "none named",
        "issues": [f"{i['title']}. {i['detail']}" for i in audit["issues"]],
        "switching": audit["switch_effort"]["text"],
    }
    if signal:
        facts["their_post"] = {"title": signal.get("title"), "quote": signal.get("quote") or (signal.get("body") or "")[:400]}
    return _strip_dashes(facts)


def _strip_dashes(value: Any) -> Any:
    """Remove dashes from facts so the model does not copy them ('pay-as-you-go' -> 'pay as you go',
    '$0.09 - $0.21' -> '$0.09 to $0.21'). URLs are left alone."""
    if isinstance(value, dict):
        return {k: _strip_dashes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_dashes(v) for v in value]
    if not isinstance(value, str) or value.startswith("http"):
        return value
    value = re.sub(r"\s+[-‐‑‒–—―]\s+", " to " if re.search(r"\d\s+[-–—]\s+\S?\d", value) else ", ", value)
    return re.sub(r"(?<=\w)[-‐‑‒–—―](?=\w)", " ", value)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUMBER_RE.findall(text)}


def _price_units(text: str) -> set[tuple[str, str]]:
    """(number, unit stem) pairs such as ('0.49', 'min') from '₹0.49 per minute'."""
    return {(n.replace(",", "").rstrip("."), u.lower()[:3]) for n, u in PRICE_UNIT_RE.findall(text)}


def check_opener(text: str, facts: dict) -> list[str]:
    """Problems with a drafted opener. Empty list means it passes."""
    problems: list[str] = []
    facts_text = json.dumps(facts, ensure_ascii=False)
    words = text.split()
    if not words:
        return ["empty"]
    if len(words) > MAX_WORDS:
        problems.append(f"{len(words)} words, max {MAX_WORDS}")
    if DASH_RE.search(text):
        problems.append("contains a dash or hyphen")
    if len(re.findall(r"\bdodo", text, re.I)) > 1:
        problems.append("mentions Dodo more than once")
    low = text.lower()
    hype = [w for w in HYPE_WORDS if re.search(rf"\b{re.escape(w)}\b", low)]
    if hype:
        problems.append(f"hype words: {', '.join(hype)}")
    if not text.rstrip().endswith("?"):
        problems.append("does not end with a question")
    if HARD_ASK_RE.search(text):
        problems.append(f"asks for a meeting ('{HARD_ASK_RE.search(text).group(0)}'); ask about their situation instead")
    for name, fp in fingerprints().items():
        if fp.get("is_mor"):
            continue
        for brand in fp.get("names", []):
            if re.search(rf"\b{re.escape(brand)}\b[^.?!]{{0,40}}\b(?:seller|merchant) of record", text, re.I) and \
                    not re.search(rf"\b{re.escape(brand)}\b[^.?!]{{0,40}}\b(?:not|isn't|is not)\b", text, re.I):
                problems.append(f"calls {brand} the seller of record; {brand} only processes payments")
    bad_units = _price_units(text) - _price_units(facts_text)
    if bad_units:
        problems.append("price units not in facts: " + ", ".join(f"{n} per {u}" for n, u in sorted(bad_units)))
    extra_numbers = _numbers(text) - _numbers(facts_text)
    if extra_numbers:
        problems.append(f"numbers not in facts: {', '.join(sorted(extra_numbers))}")
    # Provider names must come from their setup, issues or post, not the generic switching text,
    # which lists Stripe, Paddle etc. as examples.
    brand_text = json.dumps({k: v for k, v in facts.items() if k != "switching"}, ensure_ascii=False).lower()
    for name, fp in fingerprints().items():
        if name == "dodo":
            continue
        for brand in fp.get("names", []):
            if re.search(rf"\b{re.escape(brand)}\b", text, re.I) and brand.lower() not in brand_text:
                problems.append(f"names {brand}, which is not in their setup or post")
    return problems


def draft_opener(audit: dict, signal: dict | None = None, client: Any = None) -> OpenerResult:
    """Draft and check an opener. Never raises: failures come back with ok=False."""
    facts = build_facts(audit, signal)
    user = "FACTS:\n" + json.dumps(facts, ensure_ascii=False, indent=1)
    problems: list[str] = []
    text = ""
    for _ in range(2):
        prompt = user if not problems else (
            f"{user}\n\nYour previous draft was:\n{text}\nIt broke these rules: {'; '.join(problems)}. "
            "Write a new one that follows every rule.")
        try:
            text = chat_json(SYSTEM, prompt, OpenerOut, client=client).opener.strip()
        except LLMError as e:
            return OpenerResult("", False, [str(e)])
        problems = check_opener(text, facts)
        if not problems:
            return OpenerResult(text, True)
    return OpenerResult(text, False, problems)
