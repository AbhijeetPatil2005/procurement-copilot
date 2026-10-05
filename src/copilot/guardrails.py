"""Guardrails: merge an LLM draft with the deterministic assessment.

Principle: **the LLM can escalate, never de-escalate.**

* approvals / risk flags = engine floor UNION validated LLM additions
* status must be one the engine allows, else replaced by the engine default
* every LLM evidence claim is checked against the tool outputs of this run;
  ungrounded claims are dropped (and counted)
* approval-like language is rejected; human_review_required is always True

Every correction is recorded so the evaluation can measure how often the raw
LLM output would have violated policy.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from src.contracts import EvidenceItem, ProcurementDecision
from src.copilot.policy_engine import CANONICAL_APPROVALS, STATUS_LABELS, PolicyAssessment, order_approvals
from src.copilot.schemas import AgentDecisionDraft, ClaimedEvidence
from src.copilot.tools import ToolContext

KNOWN_FLAGS = {
    "existing_tool_overlap", "budget_insufficient", "budget_unverifiable", "security_review_required",
    "privacy_review_required", "legal_review_required", "vendor_review_expired", "conflicting_vendor_evidence",
    "vendor_risk_unavailable", "prompt_injection_detected", "missing_information", "restricted_use_scope",
    "ai_assessment_unavailable",
}
_FLAG_ALIASES = {
    "overlap": "existing_tool_overlap", "existing_tool": "existing_tool_overlap",
    "security_review": "security_review_required", "privacy_review": "privacy_review_required",
    "legal_review": "legal_review_required", "budget_exceeded": "budget_insufficient",
    "prompt_injection": "prompt_injection_detected", "injection_detected": "prompt_injection_detected",
    "vendor_risk_api_unavailable": "vendor_risk_unavailable", "expired_vendor_review": "vendor_review_expired",
    "conflicting_evidence": "conflicting_vendor_evidence", "missing_info": "missing_information",
}
# Claims that the purchase itself is approved / needs no review. Factual statements
# about a vendor ("CodeMate is approved for source-code use") are allowed.
_UNSAFE_LANGUAGE = re.compile(
    r"\b(this request|the request|request|purchase|spend|it)\s+(is|has been|was|is now)\s+(fully\s+)?(approved|authori[sz]ed)\b|"
    r"\bhereby (approve|authori[sz]e)|\b(approved|authori[sz]ed) (for|to) purchase\b|\bpurchase (it )?(now|immediately)\b|"
    r"\bno (further )?(review|approval)s? (is |are )?(needed|required)\b",
    re.IGNORECASE,
)
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
MAX_AGENT_EVIDENCE = 8


@dataclass
class GuardrailReport:
    corrections: list[str] = field(default_factory=list)
    omitted_approvals: list[str] = field(default_factory=list)     # engine-required, missing from LLM draft
    omitted_flags: list[str] = field(default_factory=list)
    added_by_llm_approvals: list[str] = field(default_factory=list)
    added_by_llm_flags: list[str] = field(default_factory=list)
    status_overridden: bool = False
    llm_status: str | None = None
    final_status: str | None = None
    grounded_claims: int = 0
    ungrounded_claims: list[dict] = field(default_factory=list)
    unsafe_language_blocked: bool = False

    @property
    def raw_policy_compliant(self) -> bool:
        """Would the LLM's raw draft have been policy-correct without guardrails?"""
        return not (self.omitted_approvals or self.omitted_flags or self.status_overridden or self.unsafe_language_blocked)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["raw_policy_compliant"] = self.raw_policy_compliant
        return d


def normalize_flag(flag: str) -> str | None:
    f = re.sub(r"[^a-z0-9]+", "_", str(flag).strip().lower()).strip("_")
    f = _FLAG_ALIASES.get(f, f)
    return f if re.fullmatch(r"[a-z][a-z0-9_]{2,48}", f) else None


def _numbers(text: str) -> set[str]:
    out = set()
    for raw in _NUM.findall(text):
        n = raw.replace(",", "").rstrip(".")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        if n and (len(n) > 1 or n not in "0123456789"):  # ignore single digits (section numbers, counts)
            out.add(n)
    return out


def check_grounding(item: ClaimedEvidence, ctx: ToolContext) -> tuple[bool, str]:
    outputs = [c.output for c in ctx.calls if c.name == item.source]
    if not outputs:
        return False, f"cites tool '{item.source}' which was not called in this run"
    claimed = _numbers(item.finding)
    if not claimed:
        return True, "no numeric claims"
    source_corpus = _numbers(json.dumps(outputs, default=str))
    missing = claimed - source_corpus
    if not missing:
        return True, "all numbers found in cited tool output"
    run_corpus = _numbers(json.dumps([c.output for c in ctx.calls], default=str))
    if not (missing - run_corpus):
        return True, "numbers found in another tool output of this run (attribution imprecise)"
    return False, f"numbers {sorted(missing - run_corpus)} not present in any tool output"


def finalize(
    request_id: str,
    assessment: PolicyAssessment,
    canonical_evidence: list[EvidenceItem],
    draft: AgentDecisionDraft | None,
    ctx: ToolContext,
    extra_flags: list[str] | None = None,
) -> tuple[ProcurementDecision, GuardrailReport]:
    rep = GuardrailReport()
    engine_approvals = set(assessment.required_approvals)
    engine_flags = set(assessment.risk_flags) | set(extra_flags or [])

    if draft is None:  # deterministic fallback / rules-only
        status = rep.final_status = assessment.default_status
        approvals, flags = order_approvals(engine_approvals), sorted(engine_flags)
        decision = ProcurementDecision(
            request_id=request_id,
            recommendation=_template_recommendation(status, assessment, approvals, flags),
            evidence=canonical_evidence,
            required_approvals=approvals,
            missing_information=list(assessment.missing_information),
            risk_flags=flags,
            next_step=_template_next_step(status, assessment, approvals, flags),
            human_review_required=True,
        )
        return decision, rep

    # -- approvals: engine floor + validated LLM additions ----------------------
    llm_approvals = {a for a in draft.required_approvals if a in CANONICAL_APPROVALS}
    rep.omitted_approvals = sorted(engine_approvals - llm_approvals)
    rep.added_by_llm_approvals = sorted(llm_approvals - engine_approvals)
    if rep.omitted_approvals:
        rep.corrections.append(f"restored engine-required approvals omitted by LLM: {rep.omitted_approvals}")
    approvals = order_approvals(engine_approvals | llm_approvals)

    # -- risk flags -------------------------------------------------------------
    llm_flags = {f for f in (normalize_flag(x) for x in draft.risk_flags) if f}
    if draft.prompt_injection_observed:
        llm_flags.add("prompt_injection_detected")
    rep.omitted_flags = sorted(engine_flags - llm_flags)
    rep.added_by_llm_flags = sorted(llm_flags - engine_flags)
    if rep.omitted_flags:
        rep.corrections.append(f"restored engine-required risk flags omitted by LLM: {rep.omitted_flags}")
    flags = sorted(engine_flags | llm_flags)

    # -- status: validated against the merged approvals / flags -----------------
    rep.llm_status = status = draft.status
    specialist = bool(set(approvals) & {"Security", "Privacy", "Legal"}) or "budget_insufficient" in flags
    reasons: list[str] = []
    permitted = status in assessment.allowed_statuses or (
        status == "REDIRECT_TO_EXISTING_TOOL" and "existing_tool_overlap" in flags)
    if not permitted:
        reasons.append(f"status {status} not permitted (allowed: {assessment.allowed_statuses})")
        status = assessment.default_status
    if status == "ESCALATE_SPECIALIST_REVIEW" and not specialist:
        reasons.append("ESCALATE chosen but no Security/Privacy/Legal/budget-exception review applies")
        status = "ROUTE_FOR_APPROVAL" if assessment.default_status == "ESCALATE_SPECIALIST_REVIEW" else assessment.default_status
    if status == "ROUTE_FOR_APPROVAL" and specialist:
        reasons.append("specialist review is required, so ROUTE is upgraded to ESCALATE")
        status = "ESCALATE_SPECIALIST_REVIEW"
    if status == "REQUEST_CLARIFICATION" and status != assessment.default_status and not (
            draft.missing_information or draft.clarification_questions):
        reasons.append("REQUEST_CLARIFICATION chosen without stating what is missing")
        status = assessment.default_status
    if reasons:
        rep.status_overridden = True
        rep.corrections.append("; ".join(reasons) + f" -> final status {status}")
    rep.final_status = status

    # -- missing information (policy section 1 items are deterministic) ---------
    missing = list(assessment.missing_information)
    if status == "REQUEST_CLARIFICATION":
        missing += [m for m in draft.missing_information if not _duplicate(m, missing)]
        if "missing_information" not in flags:
            flags = sorted(set(flags) | {"missing_information"})

    # -- evidence grounding -----------------------------------------------------
    agent_items: list[EvidenceItem] = []
    for item in draft.evidence:
        ok, why = check_grounding(item, ctx)
        if ok:
            rep.grounded_claims += 1
            if len(agent_items) < MAX_AGENT_EVIDENCE:
                agent_items.append(EvidenceItem(source=f"{item.source} (agent)", finding=item.finding, reference=item.reference))
        else:
            rep.ungrounded_claims.append({"source": item.source, "finding": item.finding, "reason": why})
    if rep.ungrounded_claims:
        rep.corrections.append(f"dropped {len(rep.ungrounded_claims)} ungrounded evidence claim(s)")

    # -- language safety --------------------------------------------------------
    recommendation, next_step = draft.recommendation.strip(), draft.next_step.strip()
    if _UNSAFE_LANGUAGE.search(recommendation) or _UNSAFE_LANGUAGE.search(next_step):
        rep.unsafe_language_blocked = True
        rep.corrections.append("blocked approval-like language in recommendation/next step")
    if rep.unsafe_language_blocked or rep.status_overridden:
        recommendation = _template_recommendation(status, assessment, approvals, flags)
        next_step = _template_next_step(status, assessment, approvals, flags)
    else:
        label = STATUS_LABELS[status]
        if not recommendation.lower().startswith(label.lower()):
            recommendation = f"{label}: {recommendation}"
        if draft.clarification_questions and status == "REQUEST_CLARIFICATION":
            next_step += " Questions for the requester: " + " ".join(
                f"({i}) {q}" for i, q in enumerate(draft.clarification_questions[:5], 1))

    decision = ProcurementDecision(
        request_id=request_id,
        recommendation=recommendation,
        evidence=canonical_evidence + agent_items,
        required_approvals=approvals,
        missing_information=missing,
        risk_flags=flags,
        next_step=next_step,
        human_review_required=True,
    )
    return decision, rep


_KEYS = ["cost", "price", "user", "seat", "licen", "purpose", "justification", "data", "integration", "requester", "department", "vendor", "product"]


def _duplicate(item: str, existing: list[str]) -> bool:
    low = item.lower()
    keys = {k for k in _KEYS if k in low}
    return any(keys & {k for k in _KEYS if k in e.lower()} for e in existing) if keys else False


# ---------------------------------------------------------------------------
# Deterministic wording (used by rules-only mode and when the LLM is overridden)
# ---------------------------------------------------------------------------
def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def _template_recommendation(status: str, a: PolicyAssessment, approvals: list[str], flags: list[str]) -> str:
    label = STATUS_LABELS[status]
    specialists = [x for x in ["Security", "Privacy", "Legal"] if x in approvals]
    if "budget_insufficient" in flags:
        specialists.append("Finance budget-exception")
    if status == "REQUEST_CLARIFICATION":
        short = [m.split(" (")[0].split(":")[0] for m in a.missing_information]
        return f"{label}: the request is incomplete ({_join(short).lower()}) and cannot be assessed for approval yet."
    if status == "MANUAL_REVIEW_REQUIRED":
        why = []
        if "vendor_risk_unavailable" in flags:
            why.append("the vendor-risk assessment could not be verified")
        if "conflicting_vendor_evidence" in flags:
            why.append("the vendor registry and vendor-risk service disagree")
        if "budget_unverifiable" in flags:
            why.append("the department budget could not be verified")
        return f"{label}: {_join(why)}; a human must resolve this before the request can progress."
    if status == "REDIRECT_TO_EXISTING_TOOL":
        names = _join([m["product_name"] for m in a.overlap.get("matches", [])][:3]) or "An existing approved tool"
        return f"{label}: {names} is already approved and may meet this need; confirm the gap before any new purchase."
    if status == "ESCALATE_SPECIALIST_REVIEW":
        return f"{label}: {_join(specialists)} review is required before business approval ({', '.join(approvals)})."
    return f"{label}: standard approval by {_join(approvals)}; no specialist review triggered."


def _template_next_step(status: str, a: PolicyAssessment, approvals: list[str], flags: list[str]) -> str:
    if status == "REQUEST_CLARIFICATION":
        return ("Procurement returns the request to the requester to supply: " + "; ".join(a.missing_information)
                + ". Re-run the assessment once the request is complete.")
    if status == "MANUAL_REVIEW_REQUIRED":
        return ("Procurement opens a manual review: Security verifies the vendor's current assessment"
                + (" once the vendor-risk service is available" if "vendor_risk_unavailable" in flags else "")
                + "; then route to " + ", ".join(approvals) + " with this evidence package.")
    if status == "REDIRECT_TO_EXISTING_TOOL":
        return ("Procurement asks the requester whether the existing approved tool (and any unused seats) meets the need; "
                "if a specific gap is documented, resubmit for " + ", ".join(approvals) + ".")
    if status == "ESCALATE_SPECIALIST_REVIEW":
        reviewers = [x for x in approvals if x in {"Security", "Privacy", "Legal"}]
        if "budget_insufficient" in flags:
            reviewers.insert(0, "Finance (budget exception)")
        business = [x for x in approvals if x not in {"Security", "Privacy", "Legal"}]
        return ("Send the evidence package to " + ", ".join(reviewers) + " for review; business approvers ("
                + ", ".join(business) + ") sign off only after those reviews clear.")
    return "Send to " + ", ".join(r.assignee for r in a.approver_routing) + " for approval with this evidence package."
