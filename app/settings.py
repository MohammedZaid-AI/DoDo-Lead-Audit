"""Loads .env settings and the YAML config files in one place."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
OUTPUT_DIR = ROOT / "output"
AUDIT_DIR = OUTPUT_DIR / "audits"

load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    """Read one environment variable with a default."""
    return os.getenv(name, default).strip()


def contact_email() -> str:
    return env("CONTACT_EMAIL", "not-set")


def user_agent_tag() -> str:
    """Identifying tag appended to every request's user agent."""
    return f"DodoLeadAudit/0.1 (research; contact: {contact_email()})"


def request_delay() -> float:
    return float(env("REQUEST_DELAY_SECONDS", "3") or 3)


def inr_per_usd() -> float:
    return float(env("INR_PER_USD", "88") or 88)


@lru_cache
def load_yaml(name: str) -> dict[str, Any]:
    """Load config/<name>.yaml."""
    with open(CONFIG_DIR / f"{name}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
