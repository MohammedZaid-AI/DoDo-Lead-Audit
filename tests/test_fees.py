"""Fee math, sale basis selection, comparison wording, and audit issue rules."""
from app.audit.builder import build_audit
from app.audit.fees import build_fee_table, fee, fee_for, pick_sale_basis
from app.detect.pricing import PriceSample
from app.settings import load_yaml

RATES = load_yaml("fees")


def ps(amount, currency="USD", period="month", raw=None, amount_max=None):
    return PriceSample(amount, currency, period, raw or f"{currency}{amount}/{period}", amount_max)


def test_brief_example_19_dollars():
    assert fee(19, 5.0, 0.50) == 1.45  # Paddle
    assert fee(19, 4.0, 0.40) == 1.16  # Dodo base (US, one-time)


def test_rounding_half_up():
    assert fee(25, 4.5, 0) == 1.13  # 1.125 rounds up, not to even
    assert fee(0, 4.0, 0.40) == 0.40


def test_dodo_surcharges_from_config():
    dodo = RATES["dodo"]
    assert fee_for(dodo, 19, international=False, subscription=False) == 1.16
    assert fee_for(dodo, 19, international=False, subscription=True) == 1.26   # 4.5%
    assert fee_for(dodo, 19, international=True, subscription=True) == 1.54    # 6%


def test_paddle_has_no_surcharges():
    assert fee_for(RATES["paddle"], 19, True, True) == 1.45


def test_basis_first_monthly_usd():
    b = pick_sale_basis([ps(0), ps(19, raw="$19/mo"), ps(49)])
    assert b.amount_usd == 19 and not b.is_example and b.is_subscription


def test_basis_converts_inr_and_labels_approximate():
    b = pick_sale_basis([ps(999, "INR", raw="₹999/month")], rate_inr=88)
    assert b.amount_usd == 11.35 and "approximate" in b.label


def test_basis_skips_ranges_and_non_monthly():
    b = pick_sale_basis([ps(0.09, period="minute", amount_max=0.21), ps(10, period=None)])
    assert b.is_example and "Example" in b.label


def test_stripe_table_never_claims_dodo_cheaper():
    t = build_fee_table("stripe", "Stripe", [ps(19)])
    assert [r.role for r in t.rows] == ["current", "reference", "dodo"]
    text = " ".join(t.comparison)
    assert "Stripe charges less per sale" in text and "cheaper" not in text


def test_razorpay_uses_intl_card_rate():
    t = build_fee_table("razorpay", "Razorpay", [ps(19)])
    assert t.rows[0].rate_key == "razorpay_intl_cards" and t.rows[0].us_fee == 0.57


def test_paddle_comparison_states_both_regions():
    t = build_fee_table("paddle", "Paddle", [ps(19)])
    assert [r.role for r in t.rows] == ["current", "dodo"]  # no extra reference line
    line = t.comparison[0]
    assert "$1.26 vs $1.45" in line and "$1.54 vs $1.45" in line and "more" in line


def test_unknown_provider_rate_not_on_file():
    t = build_fee_table("juspay", "Juspay", [ps(19)])
    assert t.rows[0].us_fee is None and t.rows[0].rate_text == "rate not on file"


def test_no_provider_has_no_current_row():
    t = build_fee_table(None, None, [ps(19)])
    assert [r.role for r in t.rows] == ["reference", "dodo"]
    assert "could not be verified" in t.comparison[0]


# --- audit issue rules on the hand-checked sites ---

def test_scalio_audit(site):
    a = build_audit(site("scalio_app"), with_opener=False)
    assert [i.key for i in a.issues] == ["inr_only"]
    assert a.setup.has_mor == "not verified" and a.setup.price_sample == "₹999/month"
    assert a.switch_effort["kind"] == "unknown"


def test_jarvislabs_audit(site):
    a = build_audit(site("jarvislabs_ai_pricing"), with_opener=False)
    keys = [i.key for i in a.issues]
    assert "not_mor" in keys and "no_tax_line" in keys and "india_methods" not in keys
    assert a.switch_effort["commands"] == ["npm install -g dodo-migrate", "dodo-migrate stripe"]


def test_sarvam_has_tax_line_so_no_tax_issue(site):
    a = build_audit(site("sarvam_ai_api_pricing"), with_opener=False)
    assert "no_tax_line" not in [i.key for i in a.issues]
