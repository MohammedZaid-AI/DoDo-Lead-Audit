"""Pipeline helpers: product url choice, quotes, and ranking (offline, fixtures only)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.pipeline import RunStats, pick_leads, product_url_for, signal_quote

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)


def sig(id=1, product_url=None, source="github", repo="me/app", body="", urgency="medium", days_ago=5):
    return SimpleNamespace(id=id, product_url=product_url, source=source, context={"repo": repo}, title="t",
                           body=body, is_payment_pain=True, urgency=urgency, classification={},
                           created_at=(NOW - timedelta(days=days_ago)).replace(tzinfo=None))


def test_product_url_uses_classifier_then_homepage_once():
    calls = []
    home = lambda repo: calls.append(repo) or "acme.io"
    assert product_url_for(sig(product_url="https://shop.dev"), home) == "https://shop.dev"
    s = sig()
    assert product_url_for(s, home) == "https://acme.io" and s.context["homepage_checked"]
    product_url_for(sig(product_url=None, source="hn"), home)
    assert calls == ["me/app"]


def test_product_url_skips_code_hosts_and_payment_sites():
    for url in ["https://github.com/me/app", "https://www.npmjs.com/package/x", "https://stripe.com"]:
        assert product_url_for(sig(product_url=url), lambda r: None) is None
    assert product_url_for(sig(product_url="https://myapp.vercel.app"), lambda r: None) == "https://myapp.vercel.app"


def test_quote_prefers_the_payment_sentence():
    s = sig(body="We build a notes app. Stripe is not available in Pakistan so we need another way to get paid. Thanks!")
    assert signal_quote(s) == "Stripe is not available in Pakistan so we need another way to get paid."
    assert signal_quote(sig(body="No payment words here.")) == "t"
    assert signal_quote(sig(body="**Lemon Squeezy** is the approved `merchant of record` now.")) == \
        "Lemon Squeezy is the approved merchant of record now."


def test_pick_leads_ranks_and_keeps_best_post_per_site(site):
    bolna, scalio = site("bolna_ai_pricing"), site("scalio_app")
    by_domain = {"bolna.ai": [sig(1, urgency="low"), sig(2, urgency="high")], "scalio.app": [sig(3)]}
    urls = {"bolna.ai": "https://bolna.ai", "scalio.app": "https://scalio.app"}
    leads = pick_leads(by_domain, urls, {"bolna.ai": bolna, "scalio.app": scalio}, NOW, RunStats(), log=lambda *_: None)
    assert [l.domain for l in leads] == ["bolna.ai", "scalio.app"]
    assert leads[0].signal_id == 2 and leads[0].score > leads[1].score


def test_readme_lookup_runs_once_after_homepage():
    calls = []
    s = sig()
    url = product_url_for(s, lambda r: None, lambda r: calls.append(r) or "https://acme.app")
    assert url == "https://acme.app" and s.context["readme_checked"] and calls == ["me/app"]
    s2 = sig()
    s2.context["homepage_checked"] = s2.context["readme_checked"] = True
    assert product_url_for(s2, lambda r: 1 / 0, lambda r: 1 / 0) is None  # both already tried: no lookups
