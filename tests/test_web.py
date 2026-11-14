"""Dashboard: lists leads by score, filters, audit pages, and 'Mark reviewed' saves (temporary database)."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from app import db
from app.audit.builder import build_audit
from app.audit.render import save_audit
from app.web.main import app


@pytest.fixture
def client(monkeypatch, tmp_path, site):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    with db.session() as s:
        sig = db.Signal(source="github", url="https://github.com/a/b/issues/1", title="Stripe abroad?",
                        pain_type="mor_switch", urgency="high", is_payment_pain=True,
                        created_at=datetime.now(timezone.utc))
        s.add(sig)
        s.commit()
        s.refresh(sig)
        for slug, score, signal_id in [("bolna_ai_pricing", 8, sig.id), ("scalio_app", 2, None)]:
            audit = build_audit(site(slug), with_opener=False)
            path = save_audit(audit, out_dir=tmp_path)
            company = db.upsert_company(s, audit.company_name, audit.url, audit.domain)
            db.save_audit(s, company.id, audit, str(path), signal_id=signal_id, score=score,
                          extra={"score_reasons": ["+3 real payment pain in their post"]})
    return TestClient(app)


def test_dashboard_lists_leads_by_score(client):
    html = client.get("/").text
    assert html.index("Bolna") < html.index("Scalio")
    assert "MoR switch" in html and "https://github.com/a/b/issues/1" in html and "Open audit" in html


def test_filters(client):
    assert "Scalio" not in client.get("/?min_score=5").text
    assert "Bolna" not in client.get("/?provider=Not+verified").text
    page = client.get("/?pain_type=mor_switch").text
    assert "Bolna" in page and "Scalio" not in page
    assert "No leads match" in client.get("/?min_score=10").text
    assert client.get("/?min_score=abc").status_code == 200  # bad input is ignored, not an error


def test_audit_page_is_the_saved_file(client):
    r = client.get("/audit/1")
    assert r.status_code == 200 and "Payment audit" in r.text and "All leads" in r.text
    assert client.get("/audit/999").status_code == 404


def test_mark_reviewed_saves(client):
    assert client.post("/audit/1/reviewed", json={"reviewed": True}).json() == {"id": 1, "reviewed": True}
    with db.session() as s:
        assert s.get(db.Audit, 1).reviewed
    assert "Bolna" not in client.get("/?reviewed=no").text
    client.post("/audit/1/reviewed", json={"reviewed": False})
    with db.session() as s:
        assert not s.get(db.Audit, 1).reviewed
    assert client.post("/audit/999/reviewed", json={"reviewed": True}).status_code == 404


def test_run_button_starts_one_run_at_a_time(client, monkeypatch):
    import threading
    import time
    from types import SimpleNamespace

    from app.web import main
    from app.web.runner import Runner

    gate, calls = threading.Event(), []

    def fake_run(days, top, fetch, sources, opener, log, yc=0):
        calls.append((days, top, fetch, opener, yc))
        log("listening")
        gate.wait(5)
        return SimpleNamespace(candidates=4, sites=3, openers=2), [1, 2]

    monkeypatch.setattr(main, "runner", Runner(target=fake_run))
    r = client.post("/run", json={"days": 3, "top": 5, "fetch": False, "opener": True, "yc": 7})
    assert r.status_code == 200 and r.json()["state"] == "running"
    assert client.post("/run", json={}).status_code == 409  # second click while busy
    gate.set()
    for _ in range(50):
        s = client.get("/run/status").json()
        if s["state"] != "running":
            break
        time.sleep(0.05)
    assert s["state"] == "done" and "2 leads audited" in s["summary"] and "listening" in s["log"]
    assert calls == [(3, 5, False, True, 7)]
    assert client.post("/run", json={"days": 99}).status_code == 422  # out of range
    assert client.post("/run", data={"days": "3"}).status_code == 422  # plain form posts are refused
    assert 'id="run-btn"' in client.get("/").text


def test_run_failure_is_reported(client, monkeypatch):
    import time

    from app.web import main
    from app.web.runner import Runner

    def boom(*a, **k):
        raise RuntimeError("Groq daily limit")

    monkeypatch.setattr(main, "runner", Runner(target=boom))
    client.post("/run", json={})
    for _ in range(50):
        s = client.get("/run/status").json()
        if s["state"] != "running":
            break
        time.sleep(0.05)
    assert s["state"] == "failed" and "Groq daily limit" in s["summary"]


def test_redraft_only_touches_failed_openers(client, monkeypatch, site):
    from app import pipeline

    monkeypatch.setattr(pipeline, "detect_sites", lambda urls, **k: {d: site("bolna_ai_pricing") for d in urls})
    real_build = pipeline.build_audit

    def build(det, signal, with_opener):
        a = real_build(det, signal, False)
        a.opener = "Fresh opener?"
        return a

    monkeypatch.setattr(pipeline, "build_audit", build)
    monkeypatch.setattr(pipeline, "save_audit", lambda a: "x.html")
    client.post("/audit/1/reviewed", json={"reviewed": True})
    assert pipeline.redraft(log=lambda *_: None) == (1, 1)  # bolna had no opener; scalio has no signal
    with db.session() as s:
        row = s.get(db.Audit, 1)
        assert row.opener == "Fresh opener?" and row.score == 8 and row.reviewed
        assert row.data["score_reasons"] == ["+3 real payment pain in their post"]
    assert pipeline.redraft(log=lambda *_: None) == (0, 0)  # nothing missing now
