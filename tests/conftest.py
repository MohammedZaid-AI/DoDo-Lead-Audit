"""Shared test helpers: load saved page fixtures for offline tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.detect import DetectionResult, analyze_pages  # noqa: E402
from app.detect.browser import PageData  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load_site(slug: str) -> DetectionResult:
    stems = sorted({p.with_suffix("") for p in FIXTURES.glob(f"{slug}__*.json")})
    if not stems:
        pytest.skip(f"fixture {slug} missing; run: python cli.py audit-file tests/test_urls.txt --save-fixtures")
    pages = [PageData.load(s) for s in stems]
    return analyze_pages(pages[0].requested_url, pages)


@pytest.fixture
def site():
    return load_site
