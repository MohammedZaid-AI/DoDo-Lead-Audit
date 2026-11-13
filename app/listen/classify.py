"""LLM classifier: is a post a real payment pain from someone with their own product?

Returns the brief's JSON shape. product_url is only kept if it appears in the post
itself (or was supplied as the repo's homepage); a URL the model made up is dropped.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel

from app.listen import RawSignal
from app.llm import DailyLimitError, LLMError, chat_json

PainType = Literal["stripe_access", "mor_switch", "tax_confusion", "choosing_gateway", "payout_issue", "other"]

SYSTEM = """Screen posts for Dodo Payments, a Merchant of Record (MoR) for SaaS and digital products.

is_payment_pain = true ONLY if both:
1. The author sells or will sell THEIR OWN product (a GitHub issue in their own product repo counts).
2. The post is about WHO processes their payments or WHO is the seller of record:
   cannot get, keep or use a provider (Stripe not available in their country, rejected, invite only, closed);
   choosing or switching payment provider or MoR; VAT, GST or sales tax on their digital sales;
   selling to buyers abroad or in other currencies; payouts blocked by country.
   Planning, researching or comparing providers counts. So does setting up checkout on a provider
   they have only just picked: they are making the payments decision right now.
FALSE for maintenance on a provider they already run in production (bugs, expired keys, HTTP errors,
webhooks, refunds logic, outages, adding plans or yearly billing, UI tweaks), payment plugins for other
people's stores, advice to others, ads, payment company staff, job posts, library feature requests, news.

Examples:
"Set up regions: India with INR and Razorpay, Global with USD and Stripe" -> true, choosing_gateway
"Research: how do we sell our app to buyers outside the US" -> true, choosing_gateway
"Lemon Squeezy is now owned by Stripe, should we move?" -> true, mor_switch
"Integrate Paddle checkout for our Pro plan" (new setup) -> true, choosing_gateway, low
"Rotate expired Stripe API key" -> false
"Checkout returns HTTP 503 after deploy" -> false
"Add Razorpay gateway plugin for hosting panel users" -> false

pain_type: stripe_access | mor_switch | tax_confusion | choosing_gateway | payout_issue | other
urgency: high = blocked or losing sales now; medium = actively deciding; low = early planning.
product_url: their product site only if written in the post, else null. Never guess.
one_line_reason: one plain sentence.

JSON keys: is_payment_pain, pain_type, urgency, sells_digital_product, product_url, one_line_reason.
Respond only with JSON."""


class Classification(BaseModel):
    is_payment_pain: bool
    pain_type: PainType
    urgency: Literal["high", "medium", "low"]
    sells_digital_product: bool
    product_url: str | None = None
    one_line_reason: str


URL_RE = re.compile(r"https?://[^\s)\]>\"']+|\b(?:[a-z0-9-]+\.)+(?:com|io|ai|app|dev|co|so|sh|in|xyz|net|org)\b[^\s)]*",
                    re.I)


def _norm_host(url: str) -> str:
    u = re.sub(r"^https?://", "", url.strip().lower())
    u = u[4:] if u.startswith("www.") else u
    return u.split("/")[0]


def urls_in(text: str) -> set[str]:
    return {_norm_host(u) for u in URL_RE.findall(text or "")}


def build_input(sig: RawSignal) -> str:
    ctx = {k: v for k, v in sig.context.items() if k in ("kind", "repo", "author_association", "story_url")}
    return json.dumps({"source": sig.source, "title": sig.title, "body": sig.body[:1200], "context": ctx},
                      ensure_ascii=False, indent=1)


def check_product_url(cls: Classification, sig: RawSignal) -> Classification:
    """Drop a product_url that does not appear in the post text or its links."""
    if not cls.product_url:
        return cls
    seen = urls_in(f"{sig.title}\n{sig.body}\n{sig.context.get('story_url') or ''}")
    if _norm_host(cls.product_url) not in seen:
        cls.product_url = None
    elif not cls.product_url.startswith("http"):
        cls.product_url = "https://" + cls.product_url
    return cls


PAYMENT_TERMS = re.compile(r"\b(?:stripe|paddle|lemon\s?squeezy|razorpay|cashfree|payu|juspay|paypal|polar|gumroad|"
                           r"fastspring|chargebee|dodo payments|merchant of record|payment gateway|payment provider|"
                           r"billing provider|checkout provider)\b", re.I)
PAIN_TERMS = re.compile(
    r"\b(?:not available|unavailable|not supported|invite only|rejected|banned|suspended|closed my account|"
    r"alternative|switch(?:ing)?|migrat\w+|moving (?:to|off|from)|replace|"
    r"international|global(?:ly)?|abroad|overseas|outside (?:the )?(?:us|india|eu)|cross[- ]border|usd|"
    r"vat|gst|sales tax|tax compliance|merchant of record|\bmor\b|"
    r"payout|settlement|kyc|eligib\w+|approval|onboarding|which (?:payment|provider|gateway)|"
    r"choos\w+|evaluat\w+|compar\w+|decide|decision|options? for (?:payments|billing))\b", re.I)


def worth_classifying(sig: RawSignal) -> bool:
    """Cheap pre-filter before the LLM: mentions a payment term and a pain or decision term."""
    text = f"{sig.title}\n{sig.body}"
    return bool(PAYMENT_TERMS.search(text) and PAIN_TERMS.search(text))


def classify(sig: RawSignal, client: Any = None) -> Classification | None:
    """Classify one post. Returns None if the LLM call failed (item marked failed)."""
    try:
        cls = chat_json(SYSTEM, build_input(sig), Classification, client=client, max_tokens=700)
    except DailyLimitError:
        raise  # stop the whole batch; nothing more can be classified today
    except LLMError:
        return None
    return check_product_url(cls, sig)
