"""Renders an Audit to a standalone HTML file (inline CSS, no server needed)."""
from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.audit.builder import Audit
from app.settings import AUDIT_DIR, ROOT

TEMPLATES = ROOT / "app" / "web" / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
_env.filters["money"] = lambda x: "n/a" if x is None else f"${x:,.2f}"


def audit_slug(audit: Audit) -> str:
    return re.sub(r"[^a-z0-9]+", "-", audit.domain.lower()).strip("-")


def render_audit(audit: Audit) -> str:
    return _env.get_template("audit.html").render(a=audit)


def save_audit(audit: Audit, out_dir: Path = AUDIT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{audit_slug(audit)}.html"
    path.write_text(render_audit(audit), encoding="utf-8")
    return path
