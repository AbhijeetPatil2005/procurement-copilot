from __future__ import annotations

from src.contracts import Architecture, ProcurementDecision
from src.copilot.pipeline import CaseResult, analyze


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter used by the public/hidden evaluation harness.

    architecture="single" -> Architecture A: single tool-using agent + deterministic guardrails
    architecture="staged" -> Architecture B: Analyst agent -> policy engine -> Reviewer agent + guardrails

    If no LLM provider is configured (or it fails), both degrade to the deterministic
    policy result, flagged `ai_assessment_unavailable` - never to a favourable guess.
    """
    if architecture not in ("single", "staged"):
        raise ValueError(f"architecture must be 'single' or 'staged', got {architecture!r}")
    return analyze(request_id, architecture=architecture).decision


def handle_request_with_trace(request_id: str, architecture: str = "single", **kwargs) -> CaseResult:
    """Same as handle_request but returns the full trace (used by the UI and eval suite)."""
    return analyze(request_id, architecture=architecture, **kwargs)  # type: ignore[arg-type]
