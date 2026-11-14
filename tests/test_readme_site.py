"""Picking a product site from a README: only links that clearly are the product, never badges or docs."""
from app.listen.github import readme_site

README = """# Acme Invoices
[![build](https://img.shields.io/badge/build-passing-green)](https://github.com/acme/invoices/actions)
[![npm](https://img.shields.io/npm/v/acme)](https://www.npmjs.com/package/acme)

Invoice tool for freelancers. Built with [Next.js](https://nextjs.org) and [Stripe](https://stripe.com).

**Live demo:** [invoices.acme.dev](https://invoices.acme.dev/login)
Docs: https://acme.readthedocs.io
"""


def test_picks_the_live_link():
    assert readme_site(README, "acme/invoices") == "https://invoices.acme.dev"


def test_domain_matching_the_repo_counts_without_a_label():
    text = "Read more at <a href='https://www.paywall-kit.com/features'>our page</a>."
    assert readme_site(text, "someone/paywall-kit") == "https://paywall-kit.com"


def test_unrelated_links_without_a_site_label_are_ignored():
    text = "Uses https://some-other-lib.org and [a blog post](https://blog.example.net/post)."
    assert readme_site(text, "me/tool") is None


def test_framework_payment_hosting_and_localhost_links_are_ignored():
    text = """Website: https://vercel.com/new
Live: http://localhost:3000
Demo app: https://stripe.com/docs
Try it: https://myshop.vercel.app"""
    assert readme_site(text, "me/shop") == "https://myshop.vercel.app"


def test_images_and_empty_text():
    assert readme_site("Website screenshot: https://acme.dev/shot.png", "acme/web") is None
    assert readme_site("", "acme/web") is None


def test_real_false_positives_stay_out():
    # each line once made a wrong pick: a dependency, a data source, an issue tracker, an author's article
    cases = {
        "me/brainiac": "self-hosted auth; deployed with [Alchemy](https://alchemy.run). TypeScript.",
        "me/collector": "Card data comes from [Scryfall](https://scryfall.com)'s bulk files, which the app reads.",
        "me/eventiva": "The production workspace was reset under https://linear.app/team/issue/EVE-242.",
        "ianjmacintosh/dreamport": "Follow [my article](https://www.ianjmacintosh.com/articles/dev-container/).",
        "me/bikes": "| `PUBLIC_URL` | address of the app, e.g. `https://app.example.com` |",
    }
    for repo, text in cases.items():
        assert readme_site(text, repo) is None, repo


def test_owner_named_home_page_counts():
    assert readme_site("Part of the [StitchFoundry](https://stitchfoundry.app) family.",
                       "StitchFoundry/makerfoundry") == "https://stitchfoundry.app"
