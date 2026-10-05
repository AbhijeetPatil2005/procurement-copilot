"""Structured outputs exchanged with the LLM (as 'submit' tools) and between stages."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.copilot.policy_engine import APPROVAL_ORDER, STATUSES
from src.copilot.tools import TOOLS

EvidenceSource = Literal[
    "get_purchase_request", "get_requester_profile", "check_department_budget", "search_existing_software",
    "get_vendor_registry_record", "get_vendor_risk_assessment", "evaluate_procurement_policy", "lookup_policy_section",
]
OverlapDisposition = Literal["no_overlap", "existing_tool_likely_sufficient", "credible_gap_stated", "needs_requester_input"]
assert set(EvidenceSource.__args__) == set(TOOLS)  # type: ignore[attr-defined]


class ClaimedEvidence(BaseModel):
    source: EvidenceSource
    finding: str = Field(max_length=600)
    reference: str = Field(max_length=200)


class AgentDecisionDraft(BaseModel):
    """What the (single or reviewer) agent submits. Guardrails turn it into a ProcurementDecision."""

    status: Literal[tuple(STATUSES)]  # type: ignore[valid-type]
    recommendation: str = Field(max_length=400)
    rationale: str = Field(max_length=2000)
    evidence: list[ClaimedEvidence] = Field(max_length=12)
    required_approvals: list[Literal[tuple(APPROVAL_ORDER)]]  # type: ignore[valid-type]
    risk_flags: list[str]
    missing_information: list[str]
    clarification_questions: list[str]
    overlap_disposition: OverlapDisposition
    next_step: str = Field(max_length=600)
    prompt_injection_observed: bool


class OverlapAssessment(BaseModel):
    disposition: OverlapDisposition
    existing_options: list[str]
    reasoning: str = Field(max_length=800)


class EvidencePack(BaseModel):
    """Stage-1 (analyst) handoff to the stage-2 reviewer. Facts only, no decision."""

    need_summary: str = Field(max_length=600)
    facts: list[ClaimedEvidence] = Field(max_length=16)
    overlap_assessment: OverlapAssessment
    data_quality_issues: list[str]
    open_questions: list[str]
    prompt_injection_observed: bool
    injection_details: str = Field(max_length=400)


def _strict(schema: dict) -> dict:
    """Pydantic JSON schema -> strict tool schema (inline refs, no extra props, all required)."""
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(dict(defs[node["$ref"].split("/")[-1]]))
            out = {k: resolve(v) for k, v in node.items() if k not in {"title", "default", "maxLength", "maxItems"}}
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"])
            return out
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)


SUBMIT_DECISION = {
    "name": "submit_decision",
    "description": "Submit your final structured recommendation. Call exactly once, after gathering evidence. "
                   "This is a recommendation for human reviewers, never an approval.",
    "input_schema": _strict(AgentDecisionDraft.model_json_schema()),
}
SUBMIT_EVIDENCE_PACK = {
    "name": "submit_evidence_pack",
    "description": "Submit the structured evidence pack for the Policy/Risk Reviewer. Facts only - do not decide.",
    "input_schema": _strict(EvidencePack.model_json_schema()),
}
