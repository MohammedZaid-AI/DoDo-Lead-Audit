"""Scoring rules from brief section 13, one test per rule, plus clamping and fixture checks."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.score import LeadFacts, facts_for, score_lead

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=6)


def s(**kw) -> float:
    kw.setdefault("posted_at", OLD)
    kw.setdefault("confidence", "high")
    return score_lead(LeadFacts(**kw), now=NOW).value


def test_baseline_is_zero():
    assert s() == 0


def test_rule1_payment_pain():
    assert s(is_payment_pain=True) == 3


def test_rule2_urgency():
    assert s(urgency="high") == 2
    assert s(urgency="medium") == 1
    assert s(urgency="low") == 0


def test_rule3_prices_in_usd():
    assert s(currencies=["USD"], currency_mode="USD only") == 2
    assert s(currencies=["INR", "USD"], currency_mode="multi currency") == 2
    assert s(currencies=["INR"], currency_mode="INR only") == 0


def test_rule4_non_mor_provider():
    for p in ["stripe", "razorpay", "cashfree", "payu", "juspay", "paypal"]:
        assert s(provider=p, has_mor=False) == 2, p
    assert s(provider="stripe", has_mor=True) == 0  # an MoR layered on top: no points


def test_rule5_lemonsqueezy_or_paddle():
    assert s(provider="lemonsqueezy", has_mor=True) == 2
    assert s(provider="paddle", has_mor=True) == 2
    assert s(provider="gumroad", has_mor=True) == 0


def test_rule6_inr_only_with_global_audience():
    assert s(currency_mode="INR only", currencies=["INR"], signal_text="We want to sell to international customers") == 1
    assert s(currency_mode="INR only", currencies=["INR"], signal_text="Add yearly plan") == 0
    assert s(currency_mode="multi currency", signal_text="global launch") == 0


def test_rule7_posted_in_last_3_days():
    assert s(posted_at=NOW - timedelta(days=2)) == 1
    assert s(posted_at=NOW - timedelta(days=4)) == 0
    assert s(posted_at=(NOW - timedelta(days=1)).replace(tzinfo=None)) == 1  # naive datetimes from SQLite


def test_rule8_already_on_dodo_is_zero():
    assert s(is_payment_pain=True, urgency="high", provider="dodo", has_mor=True) == 0
    assert s(is_payment_pain=True, provider="stripe", providers=["stripe", "dodo"]) == 0


def test_rule9_low_confidence_minus_two():
    assert s(is_payment_pain=True, confidence="low") == 1
    assert s(confidence="low") == 0  # clamped, never negative


def test_clamped_at_ten():
    assert s(is_payment_pain=True, urgency="high", currencies=["USD"], provider="stripe", has_mor=False,
             posted_at=NOW, currency_mode="INR only", signal_text="global") <= 10


def test_rule8_post_about_dodo_is_zero():
    r = score_lead(LeadFacts(is_payment_pain=True, confidence="high", urgency="high",
                             signal_text="Payments: subscriptions through Dodo Payments"), now=NOW)
    assert r.value == 0 and "Dodo" in r.reasons[0]


def test_reasons_explain_each_point():
    r = score_lead(LeadFacts(is_payment_pain=True, urgency="high", confidence="low", posted_at=OLD), now=NOW)
    assert r.reasons == ["+3 real payment pain in their post", "+2 urgency high",
                         "-2 provider detection confidence is low"]


def test_facts_from_signal_and_fixture(site):
    sig = SimpleNamespace(is_payment_pain=True, urgency="medium", classification={"sells_digital_product": True},
                          title="Stripe abroad", body="", created_at=OLD)
    f = facts_for(sig, site("bolna_ai_pricing"))
    assert f.provider == "stripe" and f.has_mor is False and "USD" in f.currencies
    assert score_lead(f, now=NOW).value == 3 + 1 + 2 + 2
