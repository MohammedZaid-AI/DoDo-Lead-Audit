"""Listener parsing, filtering, dedup, redaction and classifier guards (no network, no LLM)."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from sqlmodel import Session, SQLModel, create_engine, select

from app import db
from app.listen import PoliteClient, RawSignal, redact
from app.listen.classify import check_product_url, classify, Classification
from app.listen.github import fetch_github
from app.listen.hn import fetch_hn, hit_to_signal


def client_for(handler) -> PoliteClient:
    return PoliteClient(delay=0, transport=httpx.MockTransport(handler))


HN_HITS = [
    {"objectID": "1", "author": "founder1", "created_at_i": 1762700000, "title": "Ask HN: Stripe India invite only?",
     "story_text": "<p>We sell a SaaS at acme.io &amp; Stripe said invite only. Mail me at a@b.com</p>"},
    {"objectID": "2", "author": "stripe", "created_at_i": 1762700000, "story_title": "x", "comment_text": "promo"},
    {"objectID": "3", "author": "dev2", "created_at_i": 1762700000, "story_title": "Show HN: Tool", "story_id": 9,
     "comment_text": "Use a <i>merchant of record</i>"},
]


def test_hn_parse_story_and_comment():
    s = hit_to_signal(HN_HITS[0], "stripe india")
    assert s.url == "https://news.ycombinator.com/item?id=1" and "acme.io & Stripe" in s.body
    assert "[email removed]" in s.body and "a@b.com" not in s.body
    c = hit_to_signal(HN_HITS[2], "merchant of record")
    assert c.title == "Comment on: Show HN: Tool" and c.context["kind"] == "comment"
    assert c.body == "Use a merchant of record"


def test_fetch_hn_skips_payment_companies_and_dedupes():
    seen = []

    def handler(req):
        seen.append(req.url.params["query"])
        return httpx.Response(200, json={"hits": HN_HITS})

    sigs = fetch_hn(days=7, phrases=["stripe india", "merchant of record"], client=client_for(handler))
    assert sorted(s.url[-1] for s in sigs) == ["1", "3"]  # author 'stripe' skipped, dupes merged
    assert seen == ['"stripe india"', '"merchant of record"']  # exact phrase queries


def gh_item(n, repo="me/myapp", title="Which payment provider?", login="me", typ="User"):
    return {"html_url": f"https://github.com/{repo}/issues/{n}", "repository_url": f"https://api.github.com/repos/{repo}",
            "title": title, "body": "We sell a SaaS. Call +1 415 555 0100", "user": {"login": login, "type": typ},
            "created_at": "2026-11-10T10:00:00Z", "author_association": "OWNER"}


def test_fetch_github_filters():
    items = [gh_item(1), gh_item(2),  # same title in same repo -> collapsed
             gh_item(3, repo="stripe/stripe-node"),  # payment company org
             gh_item(4, repo="x/y", login="dependabot[bot]", typ="Bot"),
             gh_item(5, repo="other/app")]

    def handler(req):
        assert "is:issue" in req.url.params["q"] and req.url.params["q"].startswith('"merchant of record"')
        return httpx.Response(200, json={"items": items})

    sigs = fetch_github(days=7, phrases=["merchant of record"], client=client_for(handler))
    assert sorted(s.url for s in sigs) == ["https://github.com/me/myapp/issues/1", "https://github.com/other/app/issues/5"]
    assert "[phone removed]" in sigs[0].body and sigs[0].context["repo"] in ("me/myapp", "other/app")


def test_redact():
    assert redact("write to jo.doe+x@mail.co.uk or +91 98765 43210") == "write to [email removed] or [phone removed]"
    assert redact("priced at $0.09 per minute in 2026") == "priced at $0.09 per minute in 2026"


SIG = RawSignal("hn", "https://news.ycombinator.com/item?id=1", "f", "Stripe invite only",
                "Our SaaS acme.io cannot get Stripe in India.", datetime.now(timezone.utc), {})


def cls(url):
    return Classification(is_payment_pain=True, pain_type="stripe_access", urgency="high",
                          sells_digital_product=True, product_url=url, one_line_reason="r")


def test_product_url_kept_only_if_in_post():
    assert check_product_url(cls("acme.io"), SIG).product_url == "https://acme.io"
    assert check_product_url(cls("https://www.acme.io/pricing"), SIG).product_url == "https://www.acme.io/pricing"
    assert check_product_url(cls("https://made-up.com"), SIG).product_url is None


class FakeLLM:
    def __init__(self, reply):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])))


def test_classify_validates_and_guards():
    reply = json.dumps({"is_payment_pain": True, "pain_type": "stripe_access", "urgency": "high",
                        "sells_digital_product": True, "product_url": "https://invented.dev", "one_line_reason": "x"})
    out = classify(SIG, client=FakeLLM(reply))
    assert out.is_payment_pain and out.product_url is None


def test_classify_bad_enum_fails_cleanly():
    reply = json.dumps({"is_payment_pain": True, "pain_type": "weird", "urgency": "high",
                        "sells_digital_product": True, "product_url": None, "one_line_reason": "x"})
    assert classify(SIG, client=FakeLLM(reply)) is None


def test_store_signals_dedupes(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        assert db.store_signals(s, [SIG, SIG]) == 1
        assert db.store_signals(s, [SIG]) == 0
        assert len(s.exec(select(db.Signal)).all()) == 1


def test_prefilter_needs_payment_and_pain_terms():
    from app.listen.classify import worth_classifying
    mk = lambda t, b="": RawSignal("github", "u", "a", t, b, datetime.now(timezone.utc), {})
    assert worth_classifying(mk("Stripe not available in Pakistan, need an alternative"))
    assert worth_classifying(mk("Epic: USD plans and own billing for global businesses", "Move off Razorpay INR only"))
    assert not worth_classifying(mk("PATCH stripe_payments return_url before redirect"))
    assert not worth_classifying(mk("Adds zebra striping to all tables"))
