"""Central configuration.

Everything that changes behaviour lives here so the rest of the code never reads
environment variables directly. Policy constants that come from
`data/procurement_policy.md` are parsed from the policy file itself where that is
robust (reference date, review validity window); the threshold table is encoded
in code and guarded by a drift test (`tests/test_policy_drift.py`).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import src  # noqa: F401  (ensures .env is loaded before settings are read)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RUNTIME_DIR = ROOT / "runtime"
POLICY_PATH = DATA_DIR / "procurement_policy.md"

_FALLBACK_REFERENCE_DATE = date(2026, 9, 30)
_FALLBACK_REVIEW_VALIDITY_DAYS = 365


@lru_cache(maxsize=1)
def policy_reference_date() -> date:
    """Read the data-snapshot / evaluation reference date from the policy file.

    The policy is the source of truth; the machine clock is never used for
    date-based checks.
    """
    try:
        text = POLICY_PATH.read_text(encoding="utf-8")
        match = re.search(r"reference date:\*\*\s*(\d{4}-\d{2}-\d{2})", text, re.IGNORECASE)
        if match:
            return date.fromisoformat(match.group(1))
    except OSError:
        pass
    return _FALLBACK_REFERENCE_DATE


@lru_cache(maxsize=1)
def review_validity_days() -> int:
    try:
        text = POLICY_PATH.read_text(encoding="utf-8")
        match = re.search(r"current for \*\*(\d+) days\*\*", text)
        if match:
            return int(match.group(1))
    except OSError:
        pass
    return _FALLBACK_REVIEW_VALIDITY_DAYS


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else default


@dataclass(frozen=True)
class Settings:
    vendor_risk_base_url: str
    vendor_risk_timeout_s: float
    vendor_risk_retries: int
    llm_provider: str            # anthropic | openai | none
    model_name: str
    llm_effort: str              # anthropic effort level
    llm_timeout_s: float
    max_agent_turns: int
    anthropic_fallbacks: bool
    openai_base_url: str
    openai_auth_header: str      # e.g. "x-api-key" for gateways that don't accept Bearer auth

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider in {"anthropic", "openai"}


def _detect_provider() -> str:
    explicit = _env("LLM_PROVIDER").lower()
    if explicit in {"anthropic", "openai", "none"}:
        return explicit
    if _env("ANTHROPIC_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    if _env("OPENAI_API_KEY"):
        return "openai"
    return "none"


def get_settings() -> Settings:
    provider = _detect_provider()
    default_model = {"anthropic": "claude-opus-5-5", "openai": "gpt-4o-mini"}.get(provider, "")
    return Settings(
        vendor_risk_base_url=_env("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/"),
        vendor_risk_timeout_s=float(_env("VENDOR_RISK_TIMEOUT_S", "3.0") or 3.0),
        vendor_risk_retries=int(_env("VENDOR_RISK_RETRIES", "1") or 1),
        llm_provider=provider,
        model_name=_env("MODEL_NAME") or default_model,
        llm_effort=_env("LLM_EFFORT", "medium") or "medium",
        llm_timeout_s=float(_env("LLM_TIMEOUT_S", "120") or 120),
        max_agent_turns=int(_env("MAX_AGENT_TURNS", "10") or 10),
        anthropic_fallbacks=_env("ANTHROPIC_FALLBACKS", "default").lower() not in {"off", "false", "0", "none"},
        openai_base_url=_env("OPENAI_BASE_URL"),
        openai_auth_header=_env("OPENAI_AUTH_HEADER"),
    )
