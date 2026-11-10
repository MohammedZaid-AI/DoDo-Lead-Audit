"""Provider fingerprint matching: confidence rules, primary choice, and the five test sites."""
from app.detect.browser import PageData
from app.detect.providers import detect_providers, match_page


def page(url="https://example.com/pricing", **kw) -> PageData:
    kw.setdefault("html", "<html></html>")
    return PageData(requested_url=url, final_url=url, **kw)


def test_script_src_is_high_confidence():
    r = detect_providers([page(scripts=["https://js.stripe.com/v3/"])])
    assert r.primary == "stripe" and r.confidence == "high" and r.has_mor is False


def test_network_url_is_high_confidence():
    r = detect_providers([page(network_urls=["https://cdn.paddle.com/paddle/v2/paddle.js"])])
    assert r.primary == "paddle" and r.confidence == "high" and r.has_mor is True


def test_checkout_link_is_high_and_marked():
    r = detect_providers([page(links=[{"href": "https://buy.stripe.com/abc123", "text": "Buy now"}])])
    assert r.primary == "stripe" and r.confidence == "high"
    assert r.evidence[0].location == "checkout_link"


def test_plain_link_is_medium():
    r = detect_providers([page(links=[{"href": "https://www.lemonsqueezy.com/", "text": "Lemon Squeezy"}])])
    assert r.providers["lemonsqueezy"] == "medium"


def test_inline_html_only_is_low_and_not_primary():
    r = detect_providers([page(html="<footer>We compared js.stripe.com vs others</footer>")])
    assert r.primary is None and r.has_mor is None
    assert r.providers == {"stripe": "low"}


def test_plain_text_mention_is_low():
    r = detect_providers([page(text="Integrations\nSlack\nStripe\nNotion")])
    assert r.primary is None and r.providers.get("stripe") == "low"


def test_text_declaration_is_medium():
    r = detect_providers([page(text="How are payments handled?\nWe use Stripe for secure payments.")])
    assert r.primary == "stripe" and r.confidence == "medium" and r.has_mor is False


def test_negated_text_ignored():
    r = detect_providers([page(text="Do you support PayPal?\nNot currently.")])
    assert "paypal" not in r.providers


def test_two_providers_higher_confidence_wins():
    p = page(scripts=["https://assets.lemonsqueezy.com/lemon.js"], text="Powered by Stripe.")
    r = detect_providers([p])
    assert r.primary == "lemonsqueezy" and set(r.providers) == {"lemonsqueezy", "stripe"}


def test_evidence_merged_across_pages():
    a = page("https://x.com/", text="hello")
    b = page("https://x.com/pricing", iframes=["https://checkout.razorpay.com/v1/checkout.js"])
    r = detect_providers([a, b])
    assert r.primary == "razorpay" and r.evidence[0].page_url == "https://x.com/pricing"


def test_own_site_is_skipped():
    p = page("https://www.lemonsqueezy.com/", scripts=["https://assets.lemonsqueezy.com/lemon.js"])
    assert not any(e.provider == "lemonsqueezy" for e in match_page(p))


def test_nothing_found():
    r = detect_providers([page(text="Contact us for a quote")])
    assert r.primary is None and r.confidence == "low" and r.has_mor is None


# --- the five hand-checked sites (offline fixtures) ---

def test_scalio_not_verified(site):
    # Checkout (Juspay) only loads after login, so the public pages cannot verify it.
    r = site("scalio_app")
    assert r.provider.primary is None and r.provider.has_mor is None


def test_sarvam_no_provider_visible(site):
    r = site("sarvam_ai_api_pricing")
    assert r.provider.primary is None  # Stripe only appears in an integrations logo list


def test_jarvislabs_stripe_no_mor(site):
    r = site("jarvislabs_ai_pricing")
    assert r.provider.primary == "stripe" and r.provider.has_mor is False


def test_smallest_no_mor_found(site):
    r = site("smallest_ai_pricing")
    assert r.provider.has_mor is not True


def test_bolna_no_mor_found(site):
    r = site("bolna_ai_pricing")
    assert r.provider.has_mor is not True
    assert r.provider.primary in (None, "stripe")
