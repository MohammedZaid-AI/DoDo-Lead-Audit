"""Currency, price, tax hint and local payment method extraction."""
from app.detect.pricing import (analyze_pricing, currency_mode, find_currencies, find_local_methods,
                                find_prices, find_switcher_currencies)


def test_inr_monthly_price():
    s = find_prices("Pro ₹9,999 / month")[0]
    assert (s.amount, s.currency, s.period) == (9999.0, "INR", "month")


def test_usd_mo_price():
    s = find_prices("Starter $19/mo")[0]
    assert (s.amount, s.currency, s.period) == (19.0, "USD", "month")


def test_usd_range_per_minute():
    s = find_prices("Agents $0.09 - $0.21/minute")[0]
    assert (s.amount, s.amount_max, s.period) == (0.09, 0.21, "minute")
    assert s.raw_text == "$0.09 - $0.21/minute"


def test_rs_and_code_prices():
    assert find_prices("Rs. 499 per month")[0].currency == "INR"
    assert find_prices("€29 per user")[0].period == "user"


def test_k_suffix():
    assert find_prices("Enterprise $1.5K")[0].amount == 1500.0


def test_price_a_month_does_not_eat_words():
    s = find_prices("$10 and more")[0]
    assert s.period is None


def test_dollar_without_number_ignored():
    assert find_currencies("const x = `${name}`") == []


def test_currency_modes():
    assert currency_mode(["INR"], False) == "INR only"
    assert currency_mode(["USD"], False) == "USD only"
    assert currency_mode(["USD", "INR"], False) == "multi currency"
    assert currency_mode(["USD"], True) == "multi currency"
    assert currency_mode([], False) == "unclear"


def test_switcher_from_controls():
    assert find_switcher_currencies(["₹\nINR", "$\nUSD", "Sign In"]) == ["INR", "USD"]
    r = analyze_pricing("GPU $1.00/hr", ["₹\nINR", "$\nUSD"])
    assert r.has_currency_switcher and r.currency_mode == "multi currency"


def test_tax_hints():
    r = analyze_pricing("Prices exclude GST. EU customers pay VAT. $10 plus taxes. Inclusive of all taxes.")
    assert set(r.tax_hints) == {"GST", "VAT", "excl. tax", "plus taxes", "inclusive of taxes"}


def test_local_methods_found():
    found = find_local_methods("Pay with UPI, net banking, cards and wallets.")
    assert {"UPI", "netbanking", "wallets"} <= set(found)


def test_local_methods_negated_question():
    found = find_local_methods("Do you support UPI, Paypal, etc?\nNot currently.")
    assert "UPI" not in found


def test_wallet_balance_is_not_a_method():
    assert "wallets" not in find_local_methods("Any unused wallet balance carries over.")


# --- the five hand-checked sites (offline fixtures) ---

def test_tax_at_checkout_hint():
    assert "taxes shown at checkout" in analyze_pricing("Final pricing, taxes and offers are shown at checkout.").tax_hints


def test_scalio_inr_only(site):
    p = site("scalio_app").pricing
    assert p.currency_mode == "INR only"
    assert any(s.amount == 999 and s.period == "month" for s in p.price_samples)
    assert "taxes shown at checkout" in p.tax_hints


def test_sarvam_inr_only(site):
    p = site("sarvam_ai_api_pricing").pricing
    assert p.currency_mode == "INR only"
    assert all(s.currency == "INR" for s in p.price_samples)


def test_jarvislabs_usd_with_inr_switch(site):
    p = site("jarvislabs_ai_pricing").pricing
    assert p.currency_mode == "multi currency" and p.has_currency_switcher
    assert "UPI" not in p.local_methods  # FAQ says UPI is not supported


def test_smallest_usd_per_minute(site):
    p = site("smallest_ai_pricing").pricing
    assert p.currency_mode == "USD only"
    assert any(s.amount == 0.09 and s.amount_max == 0.21 and s.period == "minute" for s in p.price_samples)


def test_bolna_credits_both_currencies(site):
    p = site("bolna_ai_pricing").pricing
    assert p.currency_mode == "multi currency" and {"USD", "INR"} <= set(p.currencies_seen)
    assert any(s.amount == 10 and s.amount_max == 5000 for s in p.price_samples)
