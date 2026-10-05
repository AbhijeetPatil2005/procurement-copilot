"""Deterministic procurement policy engine.

Pure function of the gathered facts -> `PolicyAssessment`. No LLM, no I/O.
Everything that policy defines as a threshold, trigger, or date rule is decided
here, and the LLM layer can only *add* caution on top of it (see guardrails.py).

Each rule hit cites the policy section it implements so reviewers can audit it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
APPROVAL_ORDER = ["Manager", "Department Head", "Procurement", "Finance", "CFO", "Security", "Privacy", "Legal"]
CANONICAL_APPROVALS = set(APPROVAL_ORDER)

Status = Literal[
    "REQUEST_CLARIFICATION",
    "MANUAL_REVIEW_REQUIRED",
    "REDIRECT_TO_EXISTING_TOOL",
    "ESCALATE_SPECIALIST_REVIEW",
    "ROUTE_FOR_APPROVAL",
]
STATUSES: list[str] = list(Status.__args__)  # type: ignore[attr-defined]

STATUS_LABELS = {
    "REQUEST_CLARIFICATION": "Request clarification",
    "MANUAL_REVIEW_REQUIRED": "Manual review required",
    "REDIRECT_TO_EXISTING_TOOL": "Evaluate existing tool first",
    "ESCALATE_SPECIALIST_REVIEW": "Escalate for specialist review",
    "ROUTE_FOR_APPROVAL": "Route for approval",
}

# Financial thresholds - policy section 4 (guarded by tests/test_policy_drift.py)
THRESHOLDS: list[tuple[float | None, list[str], str]] = [
    (1_000.00, ["Manager"], "Up to $1,000"),
    (10_000.00, ["Department Head", "Procurement"], "$1,000.01 - $10,000"),
    (25_000.00, ["Department Head", "Finance", "Procurement"], "$10,000.01 - $25,000"),
    (None, ["Department Head", "Finance", "CFO", "Procurement"], "Above $25,000"),
]
LEGAL_NEW_VENDOR_THRESHOLD = 10_000.00  # policy section 7 ("$10,000 or more")
MIN_PURPOSE_WORDS = 6

# Data classes (policy sections 5 & 6)
PII_CLASSES = {"employee_pii", "customer_pii"}
SECURITY_CLASSES = {"source_code", "confidential_documents", "employee_pii", "customer_pii",
                    "credentials", "secrets", "credentials_secrets", "production_data", "production_access"}
LOW_RISK_CLASSES = {"none", "public", "internal", "internal_documents", "internal_marketing",
                    "internal_finance", "production_telemetry", "telemetry"}
UNKNOWN_CLASSES = {"", "unknown", "tbd", "n/a", "na", "not sure", "unsure", "unspecified"}

# Integration patterns -> implied sensitivity (policy section 5 / 6)
_INTEGRATION_RULES: list[tuple[str, str, bool, bool]] = [
    # (regex, description, security, privacy)
    (r"\bprod(uction)?\b|cloud[\s-]*account|\baws\b|\bazure\b|\bgcp\b|kubernetes|\bk8s\b|\bdatabase\b|data[\s-]*warehouse",
     "production or cloud-account integration", True, False),
    (r"\bgit\b|github|gitlab|bitbucket|repositor(y|ies)\b(?!.*document)|source[\s-]*code",
     "source-code repository access", True, False),
    (r"vault|secrets?\b|credential|password",
     "credentials / secrets access", True, False),
    (r"\bcrm\b|salesforce|hubspot|helpdesk|helpdeskly|zendesk|ticket",
     "integration with a customer-data system (customer PII)", True, True),
    (r"\bhris\b|workday|payroll|bamboohr|\bhr\s*system|people[\s-]*system",
     "integration with an HR system (employee PII)", True, True),
    (r"document[\s-]*repositor|sharepoint|google[\s-]*drive|dropbox|file[\s-]*share|contract[\s-]*repositor",
     "document-repository integration (confidential documents)", True, False),
]
_INTEGRATION_COMPILED = [(re.compile(p, re.IGNORECASE), d, s, pv) for p, d, s, pv in _INTEGRATION_RULES]

EXTENSION_KEYWORDS = re.compile(
    r"\b(add[\s-]?on|addon|expansion|expand|additional|extra|seats?|licen[cs]es?|upgrade|advanced|training|renewal|pack|top[\s-]?up)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------
class RuleHit(BaseModel):
    rule: str
    section: str
    finding: str
    adds_approvals: list[str] = Field(default_factory=list)
    adds_flags: list[str] = Field(default_factory=list)


class ApproverRoute(BaseModel):
    role: str
    assignee: str
    basis: str


class PolicyAssessment(BaseModel):
    reference_date: str
    annual_amount_usd: float | None
    approval_tier: str
    required_approvals: list[str]
    risk_flags: list[str]
    missing_information: list[str]
    missing_fields: list[str]
    rule_hits: list[RuleHit]
    open_items: list[str]
    budget: dict
    vendor_review: dict
    data_sensitivity: dict
    overlap: dict
    injection: dict
    default_status: str
    allowed_statuses: list[str]
    status_reasons: list[str]
    approver_routing: list[ApproverRoute]


@dataclass
class PolicyFacts:
    request: dict
    employee: dict | None
    manager: dict | None
    department_head: dict | None
    budget: dict | None
    catalog_matches: list[dict]
    vendor_registry: dict | None
    vendor_risk: dict              # VendorRiskResult.to_dict()
    injection_findings: list[dict]
    reference_date: date
    review_validity_days: int = 365
    extra_notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def order_approvals(items: set[str] | list[str]) -> list[str]:
    s = set(items)
    return [a for a in APPROVAL_ORDER if a in s] + sorted(a for a in s if a not in CANONICAL_APPROVALS)


def approvals_for_amount(amount: float) -> tuple[list[str], str]:
    for ceiling, approvals, label in THRESHOLDS:
        if ceiling is None or amount <= ceiling:
            return list(approvals), label
    raise AssertionError("unreachable")


def classify_data_access(value: object) -> tuple[str, str]:
    """Return (category, normalised value). category in pii|security|low|unknown."""
    raw = (str(value).strip().lower() if value is not None else "")
    norm = re.sub(r"[\s-]+", "_", raw)
    if norm in UNKNOWN_CLASSES:
        return "unknown", norm or "unknown"
    if norm in PII_CLASSES or "pii" in norm or "personal" in norm:
        return "pii", norm
    if norm in SECURITY_CLASSES or any(k in norm for k in ("source", "confidential", "secret", "credential", "password", "restricted")):
        return "security", norm
    if norm in LOW_RISK_CLASSES or norm.startswith("internal") or "telemetry" in norm:
        return "low", norm
    return "unrecognized", norm


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _norm_status(value: object) -> str:
    v = str(value or "").strip().lower().replace(" ", "_")
    if v in {"", "none", "null"}:
        return "missing"
    if v in {"approved", "current", "passed"}:
        return "approved"
    if v in {"expired", "lapsed"}:
        return "expired"
    if v in {"pending", "not_completed", "in_progress", "incomplete", "draft", "new"}:
        return "not_completed"
    if v in {"rejected", "failed", "denied"}:
        return "rejected"
    if v in {"unknown"}:
        return "unknown"
    return v


def _money(x: float | None) -> str:
    return "unknown" if x is None else f"${x:,.2f}".replace(".00", "")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def evaluate(f: PolicyFacts) -> PolicyAssessment:  # noqa: C901 - deliberately linear, mirrors the policy
    req = f.request or {}
    approvals: set[str] = set()
    flags: set[str] = set()
    hits: list[RuleHit] = []
    missing: list[str] = []
    missing_fields: list[str] = []
    open_items: list[str] = list(f.extra_notes)

    def hit(rule: str, section: str, finding: str, adds_approvals: list[str] | None = None, adds_flags: list[str] | None = None) -> None:
        approvals.update(adds_approvals or [])
        flags.update(adds_flags or [])
        hits.append(RuleHit(rule=rule, section=section, finding=finding,
                            adds_approvals=adds_approvals or [], adds_flags=adds_flags or []))

    # -- Section 9: untrusted content -------------------------------------
    injection = {"detected": bool(f.injection_findings), "findings": f.injection_findings}
    if f.injection_findings:
        fields = sorted({x["field"] for x in f.injection_findings})
        hit("prompt_injection", "Policy §9",
            f"Instruction-like text found in business data ({', '.join(fields)}); treated as data and ignored.",
            adds_flags=["prompt_injection_detected"])

    # -- Section 1: required information ----------------------------------
    from src.copilot.injection import strip_injected_sentences  # local import avoids cycle

    def need(field_key: str, label: str) -> None:
        missing_fields.append(field_key)
        missing.append(label)

    if not f.employee:
        need("requester", f"Requester identity: requester_id '{req.get('requester_id')}' is not in the employee directory")
    elif not f.employee.get("department"):
        need("department", "Requester department")
    if not (req.get("product_name") or "").strip():
        need("product_name", "Product name")
    if not (req.get("vendor_name") or "").strip():
        need("vendor_name", "Vendor name")
    amount = req.get("annual_cost_usd")
    if isinstance(amount, str):
        try:
            amount = float(amount.replace(",", "").replace("$", ""))
        except ValueError:
            amount = None
    if amount is None or (isinstance(amount, (int, float)) and amount < 0):
        need("annual_cost_usd", "Annual cost (or a reasonable annual estimate)")
        amount = None
    users = req.get("user_count")
    if users is None or (isinstance(users, (int, float)) and users <= 0):
        need("user_count", "Number of users / licenses")
    genuine_purpose = strip_injected_sentences(req.get("business_justification"))
    if len(re.findall(r"[A-Za-z]{2,}", genuine_purpose)) < MIN_PURPOSE_WORDS:
        need("business_justification", "Business purpose (no substantive business need is stated once embedded instructions are disregarded)")
    data_cat, data_norm = classify_data_access(req.get("data_access_level"))
    if data_cat == "unknown":
        need("data_access_level", "Intended data-access level")
    if req.get("requested_integrations") is None:
        need("requested_integrations", "Required integrations (state 'none' if not applicable)")
    if missing:
        hit("required_information", "Policy §1",
            f"Request is not ready for approval: {len(missing)} required item(s) missing.",
            adds_flags=["missing_information"])

    # -- Section 4: financial thresholds ----------------------------------
    if amount is not None:
        tier_approvals, tier = approvals_for_amount(float(amount))
        hit("approval_threshold", "Policy §4",
            f"Annual amount {_money(float(amount))} falls in tier '{tier}' -> {', '.join(tier_approvals)}.",
            adds_approvals=tier_approvals)
    else:
        tier = "Undetermined (annual cost missing)"
        hit("approval_threshold", "Policy §4",
            "Approval tier cannot be determined until an annual cost is provided; Procurement owns the clarification.",
            adds_approvals=["Procurement"])

    # -- Section 2: budget -------------------------------------------------
    department = (f.employee or {}).get("department")
    budget_info: dict = {"department": department, "available_usd": None, "requested_usd": amount,
                         "remaining_after_usd": None, "within_budget": None}
    if f.budget is None:
        if department:
            hit("budget_unverifiable", "Policy §2 / §10",
                f"No software budget record exists for department '{department}'; budget cannot be verified.",
                adds_approvals=["Finance"], adds_flags=["budget_unverifiable"])
            open_items.append(f"Budget owner/record for department '{department}' must be confirmed by Finance.")
    else:
        available = float(f.budget["available_usd"])
        budget_info["available_usd"] = available
        if amount is not None:
            remaining = available - float(amount)
            budget_info["remaining_after_usd"] = remaining
            budget_info["within_budget"] = remaining >= 0
            if remaining < 0:
                hit("budget_insufficient", "Policy §2",
                    f"Cost {_money(float(amount))} exceeds {department} available software budget {_money(available)} "
                    f"(shortfall {_money(-remaining)}); Finance budget-exception review required.",
                    adds_approvals=["Finance"], adds_flags=["budget_insufficient"])
            else:
                hits.append(RuleHit(rule="budget_within", section="Policy §2",
                                    finding=f"Cost {_money(float(amount))} is within {department} available budget "
                                            f"{_money(available)} ({_money(remaining)} remaining). This does not imply approval."))

    # -- Data sensitivity (sections 5, 6) ---------------------------------
    sens_reasons_security: list[str] = []
    sens_reasons_privacy: list[str] = []
    if data_cat == "pii":
        sens_reasons_security.append(f"data access '{data_norm}' (personal data)")
        sens_reasons_privacy.append(f"tool will process {data_norm.replace('_', ' ')}")
    elif data_cat == "security":
        sens_reasons_security.append(f"data access '{data_norm}'")
    elif data_cat == "unknown":
        sens_reasons_security.append("data-access level is unknown, so sensitive access cannot be ruled out")
    elif data_cat == "unrecognized":
        open_items.append(f"Data-access level '{data_norm}' is not a recognised policy data class; confirm classification.")
    integrations = req.get("requested_integrations") or []
    for integ in integrations:
        for rx, desc, sec, priv in _INTEGRATION_COMPILED:
            if rx.search(str(integ)):
                if sec:
                    sens_reasons_security.append(f"'{integ}' = {desc}")
                if priv:
                    sens_reasons_privacy.append(f"'{integ}' = {desc}")
                break
    sensitive_data = data_cat in {"pii", "security", "unknown"} or bool(sens_reasons_security)

    # -- Vendor assessment currency + conflicts (section 5, 10) ------------
    reg = f.vendor_registry
    risk = f.vendor_risk or {}
    api_ok = bool(risk.get("ok"))
    api = risk.get("data") or {}
    reg_status = _norm_status(reg.get("security_status")) if reg else "missing"
    reg_date = _parse_date(reg.get("security_review_date")) if reg else None
    api_status = _norm_status(api.get("security_review_status")) if api_ok else "unavailable"
    api_date = _parse_date(api.get("last_review_date")) if api_ok else None

    def age(d: date | None) -> int | None:
        return (f.reference_date - d).days if d else None

    reg_age, api_age = age(reg_date), age(api_date)
    validity = f.review_validity_days
    reg_current = reg_status == "approved" and reg_age is not None and reg_age <= validity
    api_current = api_ok and api_status == "approved" and api_age is not None and api_age <= validity

    vendor_review = {
        "registry_status": reg.get("security_status") if reg else None,
        "registry_review_date": reg.get("security_review_date") if reg else None,
        "registry_days_since_review": reg_age,
        "api_status": api.get("security_review_status") if api_ok else None,
        "api_review_date": api.get("last_review_date") if api_ok else None,
        "api_days_since_review": api_age,
        "api_outcome": risk.get("status", "not_called"),
        "validity_days": validity,
        "assessment_current": None,
        "conflicts": [],
    }

    if not reg:
        hit("vendor_not_in_registry", "Policy §5 / §7",
            f"Vendor '{req.get('vendor_name')}' is not in the internal vendor registry (treated as a new vendor with no approved terms).",
            adds_approvals=["Security"], adds_flags=["security_review_required"])

    if not api_ok:
        outcome = risk.get("status", "not_called")
        if outcome == "not_found":
            hit("vendor_risk_missing", "Policy §5",
                "Vendor-risk service has no assessment record for this vendor; assessment is missing.",
                adds_approvals=["Security"], adds_flags=["security_review_required"])
        else:
            hit("vendor_risk_unavailable", "Policy §10",
                f"Vendor-risk service could not be verified ({outcome}: {risk.get('error') or 'no detail'}). "
                "No favourable status is inferred; routed to manual Security review.",
                adds_approvals=["Security"], adds_flags=["vendor_risk_unavailable", "security_review_required"])
            open_items.append("Re-run the vendor-risk check once the service is available.")

    # conflict detection: never silently choose one source
    conflicts: list[str] = []
    if reg and api_ok:
        comparable = {"approved", "expired", "not_completed", "rejected"}
        if reg_status in comparable and api_status in comparable and reg_status != api_status:
            conflicts.append(f"registry security_status '{reg.get('security_status')}' vs vendor-risk service "
                             f"'{api.get('security_review_status')}'")
        if reg_date and api_date and reg_date != api_date:
            conflicts.append(f"registry review date {reg_date} vs vendor-risk service {api_date}")
    vendor_review["conflicts"] = conflicts
    if conflicts:
        hit("conflicting_vendor_evidence", "Policy §5",
            "Internal registry and vendor-risk service disagree (" + "; ".join(conflicts) + "). Not resolved automatically.",
            adds_approvals=["Security"], adds_flags=["conflicting_vendor_evidence", "security_review_required"])

    expired_sources = []
    if reg_status == "approved" and reg_age is not None and reg_age > validity:
        expired_sources.append(f"registry review {reg_date} is {reg_age} days old (> {validity})")
    if api_ok and (api_status == "expired" or (api_status == "approved" and api_age is not None and api_age > validity)):
        expired_sources.append(f"vendor-risk service reports '{api.get('security_review_status')}' (review {api_date or 'n/a'})")
    if expired_sources:
        hit("vendor_review_expired", "Policy §5",
            "Vendor security assessment is expired: " + "; ".join(expired_sources) + ".",
            adds_approvals=["Security"], adds_flags=["vendor_review_expired", "security_review_required"])

    not_completed = (api_ok and api_status in {"not_completed", "rejected", "missing"}) or \
                    (reg is not None and reg_status in {"not_completed", "rejected", "missing", "unknown"} and not api_current)
    if not_completed and not expired_sources:
        detail = []
        if reg:
            detail.append(f"registry '{reg.get('security_status') or 'none'}'")
        if api_ok:
            detail.append(f"vendor-risk service '{api.get('security_review_status')}'")
        hit("vendor_assessment_not_current", "Policy §5",
            "Vendor security assessment is missing or not completed (" + ", ".join(detail) + ").",
            adds_approvals=["Security"], adds_flags=["security_review_required"])

    vendor_review["assessment_current"] = bool(api_current and (reg_current or reg is None) and not conflicts) if api_ok else None

    # data-driven security / privacy triggers
    if sens_reasons_security:
        hit("security_data_trigger", "Policy §5",
            "Security review required: " + "; ".join(dict.fromkeys(sens_reasons_security)) + ".",
            adds_approvals=["Security"], adds_flags=["security_review_required"])
    stores_outside = bool(api.get("stores_data_outside_region")) if api_ok else None
    if stores_outside and (data_cat in {"pii", "security", "unknown"} or sens_reasons_privacy):
        sens_reasons_privacy.append("vendor stores data outside the operating region while sensitive data may be involved")
    if sens_reasons_privacy:
        hit("privacy_trigger", "Policy §6",
            "Privacy review required: " + "; ".join(dict.fromkeys(sens_reasons_privacy)) + ".",
            adds_approvals=["Privacy"], adds_flags=["privacy_review_required"])
    if stores_outside is None and sensitive_data:
        open_items.append("Vendor data residency could not be verified; Privacy may need to confirm whether data leaves the region.")

    # -- Section 7: legal ---------------------------------------------------
    procurement_status = (reg or {}).get("procurement_status")
    is_new_vendor = reg is None or str(procurement_status or "").strip().lower() != "approved"
    terms = (reg or {}).get("legal_terms_status")
    if is_new_vendor and amount is not None and float(amount) >= LEGAL_NEW_VENDOR_THRESHOLD:
        hit("legal_new_vendor_spend", "Policy §7",
            f"New vendor (procurement status '{procurement_status or 'not registered'}') with annual spend "
            f"{_money(float(amount))} >= {_money(LEGAL_NEW_VENDOR_THRESHOLD)}.",
            adds_approvals=["Legal"], adds_flags=["legal_review_required"])
    elif is_new_vendor and amount is None:
        open_items.append("Legal review for a new vendor depends on annual spend, which is missing.")
    if str(terms or "").strip().lower() not in {"approved", "standard"}:
        hit("legal_terms_not_approved", "Policy §7",
            f"Vendor legal terms are not approved/standard (status: '{terms or 'none on file'}').",
            adds_approvals=["Legal"], adds_flags=["legal_review_required"])
    if stores_outside and data_cat in {"pii", "security"}:
        hit("legal_cross_region", "Policy §7",
            "Material cross-region data-processing issue: vendor stores data outside the region and the request involves sensitive data.",
            adds_approvals=["Legal"], adds_flags=["legal_review_required"])

    # -- Sections 3 & 8: existing software / AI restrictions ---------------
    extensions = [m for m in f.catalog_matches if m.get("relation") == "same_vendor_extension"]
    # Extending a product we already own is not a new overlapping purchase, so other
    # same-category tools are alternatives to the existing tool, not to this request.
    overlap_relations = {"same_product", "same_vendor"} if extensions else {"same_product", "same_vendor", "same_category"}
    overlapping = [m for m in f.catalog_matches if m.get("relation") in overlap_relations]
    justification = (req.get("business_justification") or "").lower()
    gap_stated = any(
        (m.get("product_name") or "").lower().split()[0] in justification or (m.get("vendor_name") or "").lower() in justification
        for m in overlapping
    )
    if overlapping:
        names = ", ".join(f"{m['product_name']} ({m['relation'].replace('_', ' ')}, {m.get('scope')}, {m.get('licensed_seats')} seats)" for m in overlapping)
        hit("existing_tool_overlap", "Policy §3",
            f"Approved catalog already contains: {names}. Overlap is not an automatic rejection; "
            + ("the request names an existing tool and states a gap." if gap_stated else "the request does not explain why existing tools are insufficient."),
            adds_flags=["existing_tool_overlap"])
        open_items.append("Seat utilisation of existing licences is not in the data; confirm unused capacity before buying new seats.")
    if extensions:
        names = ", ".join(m["product_name"] for m in extensions)
        hits.append(RuleHit(rule="extends_existing_agreement", section="Policy §3",
                            finding=f"Request extends an existing approved product/agreement ({names}); not a duplicate purchase."))
    restricted = [m for m in f.catalog_matches if "limited" in str(m.get("status", "")).lower()
                  and m.get("relation") in {"same_vendor", "same_vendor_extension", "same_product"}]
    if restricted and data_cat in {"pii", "security", "unknown"}:
        hit("ai_restricted_use", "Policy §8",
            f"{restricted[0]['product_name']} is '{restricted[0]['status']}' ({restricted[0].get('notes')}); prior approval "
            "does not extend to this data class.",
            adds_approvals=["Security"], adds_flags=["restricted_use_scope", "security_review_required"])

    # -- Status determination ----------------------------------------------
    specialist = bool(approvals & {"Security", "Privacy", "Legal"}) or "budget_insufficient" in flags
    evidence_gap = bool(flags & {"vendor_risk_unavailable", "conflicting_vendor_evidence", "budget_unverifiable"})
    exact_duplicate = any(m.get("relation") == "same_product" for m in overlapping)
    reasons: list[str] = []
    if missing:
        default = "REQUEST_CLARIFICATION"
        allowed = ["REQUEST_CLARIFICATION"]
        reasons.append("Material information is missing (Policy §1).")
    elif evidence_gap:
        default = "MANUAL_REVIEW_REQUIRED"
        allowed = ["MANUAL_REVIEW_REQUIRED", "REQUEST_CLARIFICATION"]
        reasons.append("Material evidence is unavailable, conflicting, or unverifiable (Policy §5, §10).")
    else:
        base = "ESCALATE_SPECIALIST_REVIEW" if specialist else "ROUTE_FOR_APPROVAL"
        # More conservative statuses are always permitted; guardrails check that an
        # escalation is backed by the approvals/flags the LLM adds.
        allowed = [base, "REQUEST_CLARIFICATION", "MANUAL_REVIEW_REQUIRED"]
        if base == "ROUTE_FOR_APPROVAL":
            allowed.append("ESCALATE_SPECIALIST_REVIEW")
        if overlapping:
            allowed.insert(0, "REDIRECT_TO_EXISTING_TOOL")
            if exact_duplicate or (not gap_stated and not specialist):
                default = "REDIRECT_TO_EXISTING_TOOL"
                reasons.append("An approved tool may already satisfy the need (Policy §3).")
            else:
                default = base
        else:
            default = base
        if specialist:
            reasons.append("Security / Privacy / Legal / budget-exception review is required before approval.")
        if default == "ROUTE_FOR_APPROVAL":
            reasons.append("Only standard business approvals are triggered.")

    for a in approvals:
        if a not in CANONICAL_APPROVALS:
            raise ValueError(f"non-canonical approval {a}")

    # -- Section 11: a human other than the requester must approve ----------
    requester_id = (f.employee or {}).get("employee_id")
    if requester_id and f.department_head and f.department_head.get("employee_id") == requester_id and "Department Head" in approvals:
        escalate_to = f"{f.manager['name']} ({f.manager['employee_id']})" if f.manager else "the next level of leadership"
        hits.append(RuleHit(rule="self_approval_conflict", section="Policy §11",
                            finding=f"Requester is the head of {department}; Department Head approval must go to {escalate_to} to avoid self-approval."))
        open_items.append(f"Requester is their own department head; route Department Head sign-off to {escalate_to}.")
    if f.employee and not f.manager and "Manager" in approvals:
        open_items.append("Requester has no manager on file; Procurement must name the approving manager.")

    return PolicyAssessment(
        reference_date=f.reference_date.isoformat(),
        annual_amount_usd=float(amount) if amount is not None else None,
        approval_tier=tier,
        required_approvals=order_approvals(approvals),
        risk_flags=sorted(flags),
        missing_information=missing,
        missing_fields=missing_fields,
        rule_hits=hits,
        open_items=list(dict.fromkeys(open_items)),
        budget=budget_info,
        vendor_review=vendor_review,
        data_sensitivity={"data_access_level": req.get("data_access_level"), "category": data_cat,
                          "security_reasons": list(dict.fromkeys(sens_reasons_security)),
                          "privacy_reasons": list(dict.fromkeys(sens_reasons_privacy)),
                          "stores_data_outside_region": stores_outside},
        overlap={"flagged": bool(overlapping), "gap_stated": gap_stated, "exact_duplicate": exact_duplicate,
                 "matches": overlapping, "extends": extensions},
        injection=injection,
        default_status=default,
        allowed_statuses=list(dict.fromkeys(allowed)),
        status_reasons=reasons,
        approver_routing=_route_approvers(order_approvals(approvals), f),
    )


def _route_approvers(approvals: list[str], f: PolicyFacts) -> list[ApproverRoute]:
    queues = {
        "Procurement": "Procurement team queue",
        "Finance": "Finance / FP&A queue",
        "CFO": "Office of the CFO (not in employee directory)",
        "Security": "Security review queue",
        "Privacy": "Privacy office queue",
        "Legal": "Legal / contracts queue",
    }
    routes: list[ApproverRoute] = []
    for role in approvals:
        if role == "Manager":
            m = f.manager
            routes.append(ApproverRoute(role=role, assignee=f"{m['name']} ({m['employee_id']})" if m else "Unresolved - requester has no manager on file",
                                        basis="Requester's reporting line (employees.csv)"))
        elif role == "Department Head":
            h = f.department_head
            routes.append(ApproverRoute(role=role, assignee=f"{h['name']} ({h['employee_id']}, {h['level']}, {h['department']})" if h else "Unresolved - no department head found",
                                        basis="Director/VP of the requesting department, else first Director/VP in the reporting chain"))
        else:
            routes.append(ApproverRoute(role=role, assignee=queues.get(role, role), basis="Functional review queue"))
    return routes
