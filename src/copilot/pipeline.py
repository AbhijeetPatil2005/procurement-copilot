"""Orchestration for the three ways a request can be processed.

* single  - Architecture A: one tool-using agent (incl. the policy-engine tool) -> guardrails
* staged  - Architecture B: Analyst agent (evidence pack) -> deterministic engine -> Reviewer agent -> guardrails
* rules   - deterministic baseline; also the automatic fallback when the LLM is unavailable

All three share the same tools, policy engine, evidence builder and guardrails,
so the evaluation isolates the effect of the agent architecture.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from src.config import get_settings, policy_reference_date
from src.contracts import ProcurementDecision, RunTelemetry
from src.copilot.agents import AgentIncomplete, AgentRun, run_agent
from src.copilot.evidence import build_evidence
from src.copilot.guardrails import GuardrailReport, finalize
from src.copilot.llm import LLMUnavailable, SessionFactory, default_session_factory, estimate_cost_usd
from src.copilot.policy_engine import PolicyAssessment
from src.copilot.prompts import ANALYST_SYSTEM, REVIEWER_SYSTEM, SINGLE_AGENT_SYSTEM, untrusted_block
from src.copilot.schemas import SUBMIT_DECISION, SUBMIT_EVIDENCE_PACK, AgentDecisionDraft, EvidencePack
from src.copilot.tools import ToolCall, ToolContext, tool_schemas
from src.data_access import DataRepository
from src.vendor_client import VendorRiskFetcher

ArchitectureName = Literal["single", "staged", "rules"]

DATA_TOOLS = ["get_purchase_request", "get_requester_profile", "check_department_budget", "search_existing_software",
              "get_vendor_registry_record", "get_vendor_risk_assessment", "lookup_policy_section"]
SINGLE_TOOLS = DATA_TOOLS + ["evaluate_procurement_policy"]
REVIEWER_TOOLS = ["lookup_policy_section"]


@dataclass
class CaseResult:
    decision: ProcurementDecision
    architecture: str
    mode: str                                  # llm | rules_only | rules_fallback
    assessment: PolicyAssessment | None
    guardrails: GuardrailReport
    tool_log: list[ToolCall]
    agent_runs: list[AgentRun] = field(default_factory=list)
    evidence_pack: dict | None = None
    draft: dict | None = None
    latency_ms: float = 0.0
    fallback_reason: str | None = None
    provider: str = "none"
    model: str = ""

    @property
    def usage(self) -> dict:
        total: dict[str, int] = {}
        for r in self.agent_runs:
            for k, v in r.usage.items():
                total[k] = total.get(k, 0) + v
        return total

    @property
    def cost_usd(self) -> float | None:
        return estimate_cost_usd(self.model, self.usage) if self.agent_runs else 0.0

    def trace(self) -> dict[str, Any]:
        return {
            "architecture": self.architecture,
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "fallback_reason": self.fallback_reason,
            "latency_ms": self.latency_ms,
            "usage": self.usage,
            "cost_usd": self.cost_usd,
            "guardrails": self.guardrails.to_dict(),
            "tool_log": [{"name": c.name, "caller": c.caller, "args": c.args, "ok": c.ok, "latency_ms": c.latency_ms,
                          "cached": c.cached} for c in self.tool_log],
            "agent_runs": [{"role": r.role, "llm_calls": r.llm_calls, "tool_calls": r.tool_calls, "usage": r.usage,
                            "llm_latency_ms": round(r.llm_latency_ms, 1), "validation_errors": r.validation_errors,
                            "transcript": r.transcript} for r in self.agent_runs],
            "evidence_pack": self.evidence_pack,
            "draft": self.draft,
            "assessment": self.assessment.model_dump() if self.assessment else None,
        }


def _telemetry(ctx: ToolContext, runs: list[AgentRun]) -> RunTelemetry:
    counted = [c for c in ctx.calls if not (c.caller == "system" and c.cached)]
    return RunTelemetry(llm_calls=sum(r.llm_calls for r in runs), tool_calls=len(counted), tool_names=[c.name for c in counted])


def _not_found(request_id: str, ctx: ToolContext) -> ProcurementDecision:
    return ProcurementDecision(
        request_id=request_id,
        recommendation="Request clarification: no purchase request with this id exists.",
        missing_information=["Purchase request record (request id not found)"],
        risk_flags=["missing_information"],
        next_step="Procurement confirms the request id with the requester; nothing can be assessed yet.",
        human_review_required=True,
        telemetry=_telemetry(ctx, []),
    )


def analyze(
    request_id: str,
    architecture: ArchitectureName = "single",
    repo: DataRepository | None = None,
    vendor_fetcher: VendorRiskFetcher | None = None,
    session_factory: SessionFactory | None | Literal[False] = None,
) -> CaseResult:
    """Process one request. `session_factory=False` forces the deterministic path."""
    start = time.perf_counter()
    settings = get_settings()
    ctx = ToolContext.default(repo=repo, vendor_fetcher=vendor_fetcher)
    factory = None if session_factory is False else (session_factory or default_session_factory(settings))
    provider, model = (settings.llm_provider, settings.model_name) if factory and session_factory is None else \
        (("scripted", "scripted") if factory else ("none", ""))

    if ctx.repo.get_request(request_id) is None:
        decision = _not_found(request_id, ctx)
        return CaseResult(decision, architecture, "rules_only", None, GuardrailReport(), ctx.calls,
                          latency_ms=_ms(start), provider=provider, model=model)

    runs: list[AgentRun] = []
    draft: AgentDecisionDraft | None = None
    pack: EvidencePack | None = None
    fallback_reason: str | None = None
    mode = "llm"

    if architecture == "rules" or factory is None:
        mode = "rules_only" if architecture == "rules" else "rules_fallback"
        if architecture != "rules":
            fallback_reason = "No LLM provider configured (set ANTHROPIC_API_KEY or OPENAI_API_KEY)."
    else:
        try:
            if architecture == "single":
                draft = _run_single(factory, ctx, request_id, runs, settings.max_agent_turns)
            elif architecture == "staged":
                pack, draft = _run_staged(factory, ctx, request_id, runs, settings.max_agent_turns)
            else:
                raise ValueError(f"Unknown architecture '{architecture}'")
        except (LLMUnavailable, AgentIncomplete) as exc:
            failed_run = getattr(exc, "run", None)
            if failed_run is not None and failed_run not in runs:
                runs.append(failed_run)
            mode, fallback_reason, draft = "rules_fallback", f"{type(exc).__name__}: {exc}", None

    # Deterministic floor - always computed by code (cached if an agent already ran it).
    engine_out = ctx.internal("evaluate_procurement_policy", request_id=request_id)
    assessment = PolicyAssessment.model_validate(engine_out["assessment"])
    evidence = build_evidence(ctx, assessment)
    extra_flags = ["ai_assessment_unavailable"] if mode == "rules_fallback" else []
    decision, report = finalize(request_id, assessment, evidence, draft, ctx, extra_flags=extra_flags)
    if mode == "rules_fallback":
        decision.next_step += " (AI assessment unavailable - deterministic policy result only; reviewer should read the evidence directly.)"
    decision.telemetry = _telemetry(ctx, runs)
    return CaseResult(
        decision=decision, architecture=architecture, mode=mode, assessment=assessment, guardrails=report,
        tool_log=list(ctx.calls), agent_runs=runs, evidence_pack=pack.model_dump() if pack else None,
        draft=draft.model_dump() if draft else None,
        latency_ms=round(_ms(start) - sum(r.wait_ms for r in runs), 1), fallback_reason=fallback_reason,
        provider=provider if mode == "llm" else ("none" if architecture == "rules" else provider), model=model,
    )


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _run_single(factory: SessionFactory, ctx: ToolContext, request_id: str, runs: list[AgentRun], max_turns: int) -> AgentDecisionDraft:
    user = (f"Assess purchase request {request_id}. Policy reference date: {policy_reference_date().isoformat()}. "
            "Gather the evidence, run the policy engine, then submit your decision.")
    session = factory(SINGLE_AGENT_SYSTEM, user, tool_schemas(SINGLE_TOOLS) + [SUBMIT_DECISION])
    draft, run = run_agent(session, ctx, "single_agent", SINGLE_TOOLS, "submit_decision", AgentDecisionDraft, max_turns)
    runs.append(run)
    return draft


def _run_staged(factory: SessionFactory, ctx: ToolContext, request_id: str, runs: list[AgentRun], max_turns: int) -> tuple[EvidencePack, AgentDecisionDraft]:
    # Stage 1 - analyst gathers evidence (no policy engine, no decision).
    user = (f"Gather and organise the evidence for purchase request {request_id}. "
            f"Policy reference date: {policy_reference_date().isoformat()}.")
    analyst = factory(ANALYST_SYSTEM, user, tool_schemas(DATA_TOOLS) + [SUBMIT_EVIDENCE_PACK])
    pack, run1 = run_agent(analyst, ctx, "analyst", DATA_TOOLS, "submit_evidence_pack", EvidencePack, max_turns)
    runs.append(run1)

    # Deterministic stage - code, not an agent.
    engine = ctx.internal("evaluate_procurement_policy", request_id=request_id)
    request = ctx.internal("get_purchase_request", request_id=request_id).get("request", {})

    # Stage 2 - reviewer decides from the structured handoff.
    review_input = (
        f"Purchase request {request_id}.\n\n"
        f"## Analyst evidence pack\n{json.dumps(pack.model_dump(), indent=1)}\n\n"
        f"## Deterministic policy engine output (binding floor)\n{json.dumps(engine.get('assessment', engine), indent=1, default=str)}\n\n"
        f"## Raw request\n{untrusted_block('purchase_request', json.dumps(request, indent=1))}\n\n"
        "Review the evidence and submit your decision."
    )
    reviewer = factory(REVIEWER_SYSTEM, review_input, tool_schemas(REVIEWER_TOOLS) + [SUBMIT_DECISION])
    draft, run2 = run_agent(reviewer, ctx, "reviewer", REVIEWER_TOOLS, "submit_decision", AgentDecisionDraft, max_turns)
    runs.append(run2)
    return pack, draft
