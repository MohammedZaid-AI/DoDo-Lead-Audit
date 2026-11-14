"""YC startup directory: page parsing, the company filter, storage, which sites get loaded, and scoring."""
import html
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine, select

from app import db, pipeline
from app.audit.opener import build_facts
from app.listen.yc import batch_year, fetch_directory, keep, parse_page
from app.settings import load_yaml

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
CFG = load_yaml("yc")


def company(slug, tags=("b2b", "saas"), batch="w2024", status="Active", website=None):
    return {"slug": slug, "name": slug.title(), "batch_name": batch, "ycdc_status": status, "tags": list(tags),
            "website": f"https://{slug}.com" if website is None else website, "one_liner": f"{slug} for teams",
            "long_description": f"{slug} helps teams. Mail us at hello@{slug}.com"}


def page(companies, current=1, total=1):
    props = {"companies": companies, "currentPage": current, "totalPages": total, "hasMore": current < total}
    data = html.escape(json.dumps({"component": "CompanyListPage", "props": props}), quote=True)
    return f'<html><body><div data-page="{data}"></div></body></html>'


def test_parse_page_reads_embedded_json():
    props = parse_page(page([company("acme")], 1, 3))
    assert props["companies"][0]["slug"] == "acme" and props["totalPages"] == 3
    with pytest.raises(ValueError):
        parse_page("<html>no data</html>")


def test_filter_keeps_recent_active_software_companies():
    assert keep(company("acme"), CFG)
    assert not keep(company("acme", website=""), CFG)
    assert not keep(company("acme", status="Acquired"), CFG)
    assert not keep(company("acme", batch="s2012"), CFG)
    assert not keep(company("acme", tags=("b2b", "fintech")), CFG)  # payment companies are not leads
    assert not keep(company("acme", tags=("grocery",)), CFG)
    assert batch_year("f2025") == 2025 and batch_year(None) is None


def test_fetch_directory_pages_through_and_dedupes():
    pages = {
        "https://www.ycombinator.com/companies/location/india": page([company("acme"), company("shop", tags=("retail",))], 1, 2),
        "https://www.ycombinator.com/companies/location/india?page=2": page([company("beta")], 2, 2),
        "https://www.ycombinator.com/companies/industry/saas": page([company("acme")], 1, 1),
    }
    got = []
    sigs = fetch_directory(["location/india", "industry/saas"], get=lambda u: got.append(u) or pages[u], log=lambda *_: None)
    assert [s.url.rsplit("/", 1)[1] for s in sigs] == ["acme", "beta"]
    assert len(got) == 3
    acme = sigs[0]
    assert acme.source == "yc" and acme.context["website"] == "https://acme.com" and acme.author is None
    assert "hello@acme.com" not in acme.body  # no emails stored


@pytest.fixture
def session(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(pipeline, "CACHE_DIR", tmp_path)
    with db.session() as s:
        yield s


def _raw(slug, batch="w2024"):
    from app.listen.yc import to_signal
    return to_signal(company(slug, batch=batch), "location/india", NOW)


def test_store_directory_skips_classifier_and_keeps_checked_at(session):
    assert pipeline.store_directory(session, [_raw("acme"), _raw("beta")]) == 2
    row = session.exec(select(db.Signal).where(db.Signal.url.endswith("/acme"))).one()
    assert row.status == "directory" and row.product_url == "https://acme.com" and row.is_payment_pain is False
    row.context = {**row.context, "checked_at": NOW.isoformat()}
    session.add(row)
    session.commit()
    assert pipeline.store_directory(session, [_raw("acme")]) == 0
    session.refresh(row)
    assert row.context["checked_at"] == NOW.isoformat()


def test_yc_candidates_uses_cache_then_limit_newest_first(session, tmp_path):
    pipeline.store_directory(session, [_raw("old", "s2019"), _raw("mid", "w2022"), _raw("new", "f2025"),
                                       _raw("cached", "w2020")])
    (tmp_path / "cached_com__0.json").write_text("{}")  # loaded a few days ago
    old = time.time() - 3 * 86400
    os.utime(tmp_path / "cached_com__0.json", (old, old))

    rows, fresh = pipeline.yc_candidates(session, 2, NOW)
    assert {d for d, _, _ in rows} == {"cached.com", "new.com", "mid.com"}
    assert fresh == {"new.com", "mid.com"}  # the two newest batches, the cached one costs nothing

    # next run soon after: the two just tried did not leave pages in the cache (say they failed to load),
    # so they wait for the two week window instead of being retried every run
    rows, fresh = pipeline.yc_candidates(session, 2, NOW + timedelta(hours=1))
    assert fresh == {"old.com"} and {d for d, _, _ in rows} == {"cached.com", "old.com"}
    assert pipeline.yc_candidates(session, 0, NOW + timedelta(days=15))[1] == set()


def test_yc_lead_gets_no_pain_points(session, site):
    pipeline.store_directory(session, [_raw("bolna")])
    sig = session.exec(select(db.Signal)).one()
    sc = pipeline.lead_score(sig, site("bolna_ai_pricing"), NOW)
    assert sc.reasons[0].startswith("from the YC directory")
    assert not any("pain" in r and r.startswith("+") for r in sc.reasons)
    assert not any("recent" in r.lower() or "days" in r for r in sc.reasons[1:])


def test_yc_signal_is_not_their_post(session, site):
    from app.audit.builder import build_audit

    pipeline.store_directory(session, [_raw("bolna", "w2024")])
    sig = session.exec(select(db.Signal)).one()
    d = pipeline.signal_dict(sig)
    assert d["title"] == "Bolna on the YC startup directory (W2024)" and d["quote"] == "bolna for teams"
    audit = build_audit(site("bolna_ai_pricing"), d, with_opener=False).to_dict()
    assert "their_post" not in build_facts(audit, d)
