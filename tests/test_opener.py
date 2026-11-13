"""LLM wrapper (retry, backoff, validation) and opener rule checks, using a fake client."""
import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.audit.builder import build_audit
from app.audit.opener import build_facts, check_opener, draft_opener
from app.llm import LLMError, chat_json


class FakeClient:
    """Returns queued replies; an Exception in the queue is raised instead."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=r))])


class RateLimited(Exception):
    status_code = 429
    response = SimpleNamespace(headers={"retry-after": "1"})


class Out(BaseModel):
    x: int


def test_chat_json_sends_json_mode_and_rule():
    c = FakeClient('{"x": 1}')
    assert chat_json("sys", "user", Out, client=c).x == 1
    kw = c.calls[0]
    assert kw["response_format"] == {"type": "json_object"} and kw["temperature"] == 0.2
    assert "Respond only with JSON" in kw["messages"][0]["content"]


def test_chat_json_retries_once_on_bad_json():
    c = FakeClient("not json", '{"x": 2}')
    assert chat_json("s", "u", Out, client=c).x == 2 and len(c.calls) == 2


def test_chat_json_fails_after_second_bad_json():
    with pytest.raises(LLMError):
        chat_json("s", "u", Out, client=FakeClient('{"y": 1}', "nope"))


def test_rate_limit_backoff_then_success():
    waits = []
    c = FakeClient(RateLimited(), RateLimited(), '{"x": 3}')
    assert chat_json("s", "u", Out, client=c, sleep=waits.append).x == 3
    assert waits == [1.0, 1.0]


def test_rate_limit_gives_up_after_three_retries():
    c = FakeClient(*[RateLimited() for _ in range(4)])
    with pytest.raises(LLMError):
        chat_json("s", "u", Out, client=c, sleep=lambda s: None)


# --- opener rules ---

FACTS = {"company": "Jarvislabs", "payment_provider": "Stripe", "price_sample": "$0.07 (unit not clear)",
         "switching": "If they are on Stripe, Paddle, Lemon Squeezy or Polar, dodo-migrate can help."}


def test_good_opener_passes():
    text = "Saw Jarvislabs uses Stripe and prices in USD and INR. Curious how you handle VAT for overseas buyers today?"
    assert check_opener(text, FACTS) == []


@pytest.mark.parametrize("text,problem", [
    ("word " * 61 + "ok?", "words"),
    ("Saw your pay-as-you-go pricing. How is tax handled?", "dash"),
    ("Dodo can help. Dodo is great?", "Dodo more than once"),
    ("A seamless way to sell. Want to chat?", "hype"),
    ("Saw your pricing. Let's book a call.", "question"),
    ("Your $49 plan looks good. How do you handle tax?", "numbers not in facts: 49"),
    ("Moving off Paddle soon?", "Paddle"),
    ("Saw Stripe is the seller of record for Bolna. How is tax going?", "calls Stripe the seller of record"),
    ("Saw your USD pricing. Would you be open to a quick chat?", "asks for a meeting"),
    ("Saw your USD pricing. Open to exploring how that could fit?", "asks for a meeting"),
])
def test_bad_openers_flagged(text, problem):
    assert any(problem in p for p in check_opener(text, FACTS))


def test_invented_price_unit_flagged():
    problems = check_opener("Your pricing shows $0.07 per token. How is tax going?", FACTS)
    assert any("price units not in facts: 0.07 per tok" in p for p in problems)


def test_price_with_context_is_given_to_model(site):
    facts = build_facts(build_audit(site("sarvam_ai_api_pricing"), with_opener=False).to_dict())
    assert facts["price_on_page"] == "Speech to text at ₹0.49 per minute"
    assert check_opener("Sarvam lists speech to text at ₹0.49 per minute. Where are most of your buyers?", facts) == []


def test_price_without_unit_is_not_given(site):
    facts = build_facts(build_audit(site("jarvislabs_ai_pricing"), with_opener=False).to_dict())
    assert facts["price_on_page"] == "not verified"
    assert any("0.07" in p for p in check_opener("Your $0.07 price is neat. How is tax going?", facts))


def test_stripe_not_mor_phrasing_passes():
    text = "Saw Jarvislabs uses Stripe, which is not a merchant of record. How do you handle VAT today?"
    assert check_opener(text, FACTS) == []


def test_not_mor_issue_names_company_as_seller(site):
    a = build_audit(site("bolna_ai_pricing"), with_opener=False)
    detail = next(i.detail for i in a.issues if i.key == "not_mor")
    assert "Stripe only processes the payment. Bolna itself is the seller of record" in detail


def test_opener_retries_with_feedback_then_passes(site):
    audit = build_audit(site("jarvislabs_ai_pricing"), with_opener=False).to_dict()
    bad = json.dumps({"opener": "Your seamless Stripe setup is amazing!"})
    good = json.dumps({"opener": "Saw Jarvislabs takes payments through Stripe. How are you handling VAT for buyers abroad?"})
    c = FakeClient(bad, good)
    r = draft_opener(audit, client=c)
    assert r.ok and "Stripe" in r.text
    assert "broke these rules" in c.calls[1]["messages"][1]["content"]


def test_opener_marked_failed_after_two_bad_drafts(site):
    audit = build_audit(site("jarvislabs_ai_pricing"), with_opener=False).to_dict()
    bad = json.dumps({"opener": "Dodo Dodo Dodo."})
    r = draft_opener(audit, client=FakeClient(bad, bad))
    assert not r.ok and r.problems


def test_facts_exclude_unverified_provider(site):
    facts = build_facts(build_audit(site("scalio_app"), with_opener=False).to_dict())
    assert facts["payment_provider"] == "not verified"
    assert any("Stripe" in p for p in check_opener("Are you on Stripe today?", facts))
