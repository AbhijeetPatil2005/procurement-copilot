"""Procurement Request Copilot - reviewer UI.

Request details -> evidence -> recommendation + human action, plus architecture
comparison, evaluation results and the reviewer audit log. Presentation only:
all decisions come from src/copilot (tools, policy engine, agents, guardrails).
"""
from __future__ import annotations

import html
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import streamlit as st

from src.audit import HUMAN_ACTIONS, read_log, record_human_action
from src.config import get_settings, policy_reference_date
from src.copilot.pipeline import CaseResult, analyze
from src.copilot.policy_engine import STATUS_LABELS
from src.copilot.report import approval_packet
from src.data_access import default_repository
from src.mock_service import is_healthy

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "evals" / "results"

st.set_page_config(page_title="Procurement Copilot", page_icon="🧾", layout="wide")

# ---------------------------------------------------------------------------
# Vocabulary for presentation
# ---------------------------------------------------------------------------
ARCH_LABELS = {"single": "A · Single agent", "staged": "B · Staged (Analyst → Reviewer)", "rules": "Rules only (baseline)"}
ARCH_SHORT = {"single": "Single agent", "staged": "Staged 2-agent", "rules": "Rules only"}

STATUS_META = {
    "ROUTE_FOR_APPROVAL": ("#059669", "✓", "Standard business approvals only"),
    "ESCALATE_SPECIALIST_REVIEW": ("#d97706", "⚑", "Specialist reviews must clear before business sign-off"),
    "REDIRECT_TO_EXISTING_TOOL": ("#2563eb", "↺", "An approved tool may already meet this need"),
    "MANUAL_REVIEW_REQUIRED": ("#dc2626", "!", "Evidence is missing, conflicting or unverifiable"),
    "REQUEST_CLARIFICATION": ("#7c3aed", "?", "The request is incomplete or unclear"),
}

FLAG_INFO = {
    "budget_insufficient": ("critical", "Cost exceeds the department's available budget"),
    "budget_unverifiable": ("critical", "No budget record exists for the department"),
    "vendor_review_expired": ("critical", "Vendor security assessment is older than 365 days"),
    "conflicting_vendor_evidence": ("critical", "Vendor registry and risk service disagree"),
    "vendor_risk_unavailable": ("critical", "Vendor-risk service could not be verified"),
    "prompt_injection_detected": ("critical", "Instructions hidden in business data were ignored"),
    "ai_assessment_unavailable": ("critical", "AI unavailable - deterministic policy result only"),
    "security_review_required": ("review", "Security must review before approval"),
    "privacy_review_required": ("review", "Personal data or out-of-region storage involved"),
    "legal_review_required": ("review", "New vendor ≥ $10k, non-standard terms or cross-region data"),
    "restricted_use_scope": ("review", "Existing approval does not cover this data use"),
    "missing_information": ("review", "Required request details are missing"),
    "existing_tool_overlap": ("info", "An approved tool may already cover this need"),
}
SEVERITY = {"critical": ("#dc2626", "Critical"), "review": ("#d97706", "Needs review"), "info": ("#2563eb", "Note")}

EVIDENCE_GROUPS = [
    ("get_purchase_request", "Request", "🧾"),
    ("get_requester_profile", "Requester", "👤"),
    ("check_department_budget", "Budget", "💰"),
    ("search_existing_software", "Existing software", "🧩"),
    ("get_vendor_registry_record", "Vendor registry", "🏢"),
    ("get_vendor_risk_assessment", "Vendor-risk service", "🛡️"),
    ("evaluate_procurement_policy", "Policy rules", "⚖️"),
    ("lookup_policy_section", "Policy text", "📘"),
]
SPECIALIST_ROLES = {"Security", "Privacy", "Legal"}
WORKFLOW = ["Employee request", "Understand need", "Gather evidence", "Recommend action", "Human review"]

# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
html, body, .stMarkdown, .stMarkdown p, .stButton button, .stTextInput input, .stTextArea textarea, label {font-family: 'Inter', system-ui, sans-serif;}
.block-container {padding-top: 1rem; padding-bottom: 3rem; max-width: 1400px;}
header[data-testid="stHeader"] {background: transparent;}
section[data-testid="stSidebar"] {border-right: 1px solid #e6e8f0;}
section[data-testid="stSidebar"] .block-container {padding-top: 1.2rem;}

.pc-hero {background: linear-gradient(120deg, #1e1b4b 0%, #312e81 45%, #4f46e5 100%); color: #fff; border-radius: 18px;
          padding: 22px 28px; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; gap: 18px; flex-wrap: wrap;}
.pc-hero h1 {font-size: 1.55rem; font-weight: 800; margin: 0; color: #fff; letter-spacing: -0.01em;}
.pc-hero p {margin: 4px 0 0 0; opacity: .82; font-size: .93rem;}
.pc-hero .chips {display: flex; gap: 8px; flex-wrap: wrap;}
.pc-chip {display: inline-flex; align-items: center; gap: 6px; padding: 5px 11px; border-radius: 999px; font-size: .78rem; font-weight: 600;
          background: rgba(255,255,255,.12); border: 1px solid rgba(255,255,255,.22); color: #fff; white-space: nowrap;}
.pc-dot {width: 8px; height: 8px; border-radius: 50%; display: inline-block;}

.pc-steps {display: flex; gap: 0; margin: 4px 0 18px 0; background: #fff; border: 1px solid #e6e8f0; border-radius: 14px; padding: 14px 18px;}
.pc-step {flex: 1; display: flex; align-items: center; gap: 10px; position: relative; font-size: .84rem; font-weight: 600; color: #94a3b8;}
.pc-step .n {width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: .8rem;
             background: #eef0f6; color: #94a3b8; flex-shrink: 0;}
.pc-step.done {color: #0f172a;} .pc-step.done .n {background: #4f46e5; color: #fff;}
.pc-step.active {color: #4f46e5;} .pc-step.active .n {background: #e0e7ff; color: #4f46e5; box-shadow: 0 0 0 3px #c7d2fe;}
.pc-step.human .n {background: #fef3c7; color: #b45309; box-shadow: 0 0 0 3px #fde68a;} .pc-step.human {color: #b45309;}
.pc-step:not(:last-child)::after {content: ""; flex: 1; height: 2px; background: #e6e8f0; margin: 0 10px;}
.pc-step.done:not(:last-child)::after {background: #a5b4fc;}

.pc-card {background: #fff; border: 1px solid #e6e8f0; border-radius: 16px; padding: 18px 20px; margin-bottom: 14px;
          box-shadow: 0 1px 2px rgba(15,23,42,.04);}
.pc-card h3 {font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; color: #64748b; margin: 0 0 12px 0; font-weight: 700;}
.pc-title {font-size: 1.25rem; font-weight: 800; color: #0f172a; margin: 0;}
.pc-sub {color: #64748b; font-size: .88rem; margin-top: 2px;}
.pc-tag {display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: .74rem; font-weight: 600; background: #eef2ff; color: #4338ca;
         margin: 2px 6px 2px 0;}

.pc-tiles {display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 14px;}
.pc-tile {background: #f8fafc; border: 1px solid #eef0f6; border-radius: 12px; padding: 10px 12px;}
.pc-tile .k {font-size: .72rem; color: #64748b; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;}
.pc-tile .v {font-size: .98rem; font-weight: 700; color: #0f172a; margin-top: 3px; word-break: break-word;}
.pc-tile .v.good {color: #059669;} .pc-tile .v.bad {color: #dc2626;} .pc-tile .v.warn {color: #d97706;}

.pc-untrusted {border: 1px dashed #f59e0b; background: #fffbeb; border-radius: 12px; padding: 12px 14px; font-size: .92rem; color: #422006; margin-top: 8px;}
.pc-untrusted .lbl {font-size: .7rem; font-weight: 700; color: #b45309; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 4px;}
.pc-alert {border-radius: 12px; padding: 12px 14px; font-size: .88rem; margin-top: 10px; background: #fef2f2; border: 1px solid #fecaca; color: #7f1d1d;}
.pc-alert b {color: #b91c1c;}

.pc-decision {border-radius: 18px; padding: 22px 24px; margin-bottom: 14px; color: #0f172a; border: 1px solid var(--c);
              background: linear-gradient(135deg, color-mix(in srgb, var(--c) 12%, #fff) 0%, #fff 70%);}
.pc-decision .head {display: flex; align-items: center; gap: 12px;}
.pc-decision .ico {width: 40px; height: 40px; border-radius: 12px; background: var(--c); color: #fff; display: flex; align-items: center;
                   justify-content: center; font-size: 1.2rem; font-weight: 800; flex-shrink: 0;}
.pc-decision .lbl {font-size: 1.25rem; font-weight: 800; color: var(--c); line-height: 1.2;}
.pc-decision .why {font-size: .8rem; color: #64748b; font-weight: 500;}
.pc-decision .body {font-size: .98rem; margin: 14px 0 0 0; line-height: 1.55;}
.pc-next {margin-top: 14px; background: #fff; border: 1px solid #e6e8f0; border-radius: 12px; padding: 12px 14px; font-size: .9rem; line-height: 1.5;}
.pc-next .lbl2 {font-size: .7rem; font-weight: 700; color: #4f46e5; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 3px;}
.pc-advisory {font-size: .75rem; color: #64748b; margin-top: 10px;}

.pc-metrics {display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px;}
.pc-metric {background: #fff; border: 1px solid #e6e8f0; border-radius: 14px; padding: 12px 14px;}
.pc-metric .k {font-size: .72rem; color: #64748b; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;}
.pc-metric .v {font-size: 1.35rem; font-weight: 800; color: #0f172a; margin-top: 2px;}
.pc-metric .s {font-size: .74rem; color: #94a3b8;}

.pc-appr {display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid #f1f3f8;}
.pc-appr:last-child {border-bottom: none;}
.pc-appr .box {width: 22px; height: 22px; border-radius: 6px; border: 2px solid #c7d2fe; flex-shrink: 0;}
.pc-appr .role {font-weight: 700; font-size: .92rem; color: #0f172a;}
.pc-appr .who {font-size: .8rem; color: #64748b;}
.pc-group {font-size: .7rem; font-weight: 700; color: #94a3b8; text-transform: uppercase; letter-spacing: .06em; margin: 10px 0 2px 0;}
.pc-group:first-child {margin-top: 0;}

.pc-flag {display: flex; gap: 10px; align-items: flex-start; padding: 8px 0; border-bottom: 1px solid #f1f3f8;}
.pc-flag:last-child {border-bottom: none;}
.pc-sev {font-size: .66rem; font-weight: 800; text-transform: uppercase; letter-spacing: .04em; padding: 3px 8px; border-radius: 6px;
         color: var(--c); background: color-mix(in srgb, var(--c) 12%, #fff); white-space: nowrap; margin-top: 1px;}
.pc-flag > div {min-width: 0;}
.pc-flag .name {font-family: ui-monospace, Menlo, monospace; font-size: .78rem; font-weight: 600; color: #0f172a; overflow-wrap: break-word;}
.pc-flag .desc {font-size: .8rem; color: #64748b;}
.pc-empty {font-size: .88rem; color: #059669; font-weight: 600;}

.pc-list {margin: 0; padding-left: 18px; font-size: .88rem; line-height: 1.6; color: #334155;}

.pc-evgroup {margin-bottom: 14px;}
.pc-evhead {display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: .9rem; color: #0f172a; margin-bottom: 6px;}
.pc-evhead .cnt {font-size: .7rem; font-weight: 700; color: #64748b; background: #f1f5f9; padding: 1px 8px; border-radius: 999px;}
.pc-ev {background: #f8fafc; border: 1px solid #eef0f6; border-radius: 10px; padding: 9px 12px; margin-bottom: 6px; font-size: .86rem; line-height: 1.5; color: #1e293b;}
.pc-ev .ref {font-family: ui-monospace, Menlo, monospace; font-size: .72rem; color: #64748b; margin-top: 3px;}
.pc-ev.ai {border-left: 3px solid #8b5cf6;}
.pc-ai {font-size: .64rem; font-weight: 800; color: #7c3aed; background: #ede9fe; padding: 1px 6px; border-radius: 5px; margin-left: 6px;}

.pc-cmp {border-radius: 16px; padding: 16px 18px; background: #fff; border: 1px solid #e6e8f0; border-top: 4px solid var(--c); height: 100%;}
.pc-cmp .arch {font-size: .74rem; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: .05em;}
.pc-cmp .st {font-size: 1.05rem; font-weight: 800; color: var(--c); margin: 4px 0 10px 0;}
.pc-diff {background: #fef3c7; color: #92400e; font-size: .66rem; font-weight: 800; padding: 1px 6px; border-radius: 5px; margin-left: 6px;}
.pc-kv {font-size: .84rem; color: #334155; line-height: 1.7;}
.pc-kv b {color: #0f172a;}

.pc-win {background: linear-gradient(120deg, #ecfdf5, #fff); border: 1px solid #a7f3d0; border-radius: 16px; padding: 16px 20px; margin-bottom: 14px;}
.pc-win .t {font-weight: 800; color: #065f46; font-size: 1.02rem;}
.pc-win .d {color: #065f46; font-size: .88rem; margin-top: 4px; opacity: .9;}
.pc-archcard {background: #fff; border: 1px solid #e6e8f0; border-radius: 16px; padding: 16px 18px;}
.pc-archcard.best {border: 2px solid #10b981;}
.pc-archcard .name {font-weight: 800; font-size: .95rem; color: #0f172a;}
.pc-archcard .big {font-size: 2rem; font-weight: 800; color: #0f172a; margin: 4px 0;}
.pc-archcard .row {display: flex; justify-content: space-between; font-size: .82rem; color: #475569; padding: 3px 0; border-top: 1px solid #f1f3f8;}

div[data-testid="stTabs"] button[role="tab"] p {font-weight: 600; font-size: .92rem;}
div[data-testid="stForm"] {background: #fff; border: 1px solid #e6e8f0; border-radius: 16px;}
div[data-testid="stExpander"] {background: #fff; border-radius: 14px;}
</style>
""", unsafe_allow_html=True)


def show(markup: str) -> None:
    """Render HTML. Lines are stripped so Markdown never treats indentation as a code block."""
    st.markdown("".join(line.strip() for line in markup.splitlines()), unsafe_allow_html=True)


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def money(v) -> str:
    return "Not provided" if v is None else f"${float(v):,.2f}".replace(".00", "")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
if "repo" not in st.session_state:
    st.session_state.repo = default_repository()
if "results" not in st.session_state:
    st.session_state.results = {}
repo = st.session_state.repo
settings = get_settings()


def run_case(request_id: str, architecture: str) -> CaseResult:
    return analyze(request_id, architecture=architecture, repo=repo.clone())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------
def hero(api_ok: bool) -> None:
    api = (f"<span class='pc-chip'><span class='pc-dot' style='background:{'#34d399' if api_ok else '#f87171'}'></span>"
           f"Vendor-risk API {'online' if api_ok else 'offline'}</span>")
    model = (f"<span class='pc-chip'>🤖 {esc(settings.model_name)}</span>" if settings.llm_enabled
             else "<span class='pc-chip'>⚙️ Deterministic mode (no LLM key)</span>")
    show(f"""
    <div class="pc-hero">
      <div><h1>🧾 Procurement Request Copilot</h1>
      <p>Gathers evidence with tools, applies policy in code, recommends the next action. Humans make every approval decision.</p></div>
      <div class="chips">{api}{model}<span class="pc-chip">📅 Policy date {policy_reference_date().isoformat()}</span></div>
    </div>""")


def stepper(done_upto: int, active: int | None = None, human: bool = False) -> str:
    parts = []
    for i, name in enumerate(WORKFLOW, start=1):
        cls = "done" if i <= done_upto else ""
        if active == i:
            cls = "active"
        if human and i == 5:
            cls = "human"
        mark = "✓" if cls == "done" else str(i)
        parts.append(f"<div class='pc-step {cls}'><span class='n'>{mark}</span><span>{name}</span></div>")
    return "<div class='pc-steps'>" + "".join(parts) + "</div>"


def request_card(request: dict, result: CaseResult | None) -> None:
    emp = repo.get_employee(request.get("requester_id")) or {}
    integ = ", ".join(request.get("requested_integrations") or []) or "None"
    users = request.get("user_count") if request.get("user_count") is not None else "Not provided"
    data_level = str(request.get("data_access_level") or "unknown")
    data_cls = "bad" if any(k in data_level for k in ("pii", "source", "confidential", "credential")) else ("warn" if data_level == "unknown" else "")
    cost_cls = "warn" if request.get("annual_cost_usd") is None else ""
    budget_tile = "<div class='pc-tile'><div class='k'>Budget fit</div><div class='v'>Run analysis</div></div>"
    if result and result.assessment:
        b = result.assessment.budget
        if b.get("within_budget") is True:
            budget_tile = f"<div class='pc-tile'><div class='k'>Budget fit</div><div class='v good'>Within · {money(b['remaining_after_usd'])} left</div></div>"
        elif b.get("within_budget") is False:
            budget_tile = f"<div class='pc-tile'><div class='k'>Budget fit</div><div class='v bad'>Short by {money(-b['remaining_after_usd'])}</div></div>"
        else:
            budget_tile = "<div class='pc-tile'><div class='k'>Budget fit</div><div class='v warn'>Cannot verify</div></div>"
    requester = f"{esc(emp.get('name', request.get('requester_id')))} · {esc(emp.get('department', 'unknown dept'))}"
    show(f"""
    <div class="pc-card">
      <h3>Purchase request · {esc(request.get('request_id'))}</h3>
      <div class="pc-title">{esc(request.get('product_name') or '(no product)')}</div>
      <div class="pc-sub">{esc(request.get('vendor_name') or '(no vendor)')} · requested by {requester}</div>
      <div style="margin-top:8px"><span class="pc-tag">{esc(request.get('category') or 'Uncategorised')}</span>
      <span class="pc-tag" style="background:#f1f5f9;color:#475569">Urgency: {esc(request.get('urgency'))}</span></div>
      <div class="pc-tiles">
        <div class="pc-tile"><div class="k">Annual cost</div><div class="v {cost_cls}">{money(request.get('annual_cost_usd'))}</div></div>
        <div class="pc-tile"><div class="k">Users</div><div class="v">{esc(users)}</div></div>
        {budget_tile}
        <div class="pc-tile"><div class="k">Data access</div><div class="v {data_cls}">{esc(data_level.replace("_", " "))}</div></div>
        <div class="pc-tile" style="grid-column: span 2"><div class="k">Integrations</div><div class="v">{esc(integ)}</div></div>
      </div>
      <div class="pc-untrusted"><div class="lbl">Business justification · untrusted text, treated as data</div>
      {esc(request.get('business_justification') or '(empty)')}</div>
    """ + _injection_alert(result) + "</div>")


def _injection_alert(result: CaseResult | None) -> str:
    if not (result and result.assessment and result.assessment.injection["detected"]):
        return ""
    found = "; ".join(f"“{esc(f['excerpt'])}”" for f in result.assessment.injection["findings"][:3])
    return f"<div class='pc-alert'><b>Embedded instructions detected and ignored:</b> {found}</div>"


def decision_card(result: CaseResult) -> None:
    d, g = result.decision, result.guardrails
    status = g.final_status or ""
    color, icon, why = STATUS_META.get(status, ("#64748b", "•", ""))
    label = STATUS_LABELS.get(status, "Recommendation")
    body = d.recommendation[len(label) + 1:].strip() if d.recommendation.startswith(label + ":") else d.recommendation
    body = body[:1].upper() + body[1:]
    show(f"""
    <div class="pc-decision" style="--c:{color}">
      <div class="head"><div class="ico">{icon}</div><div><div class="lbl">{esc(label)}</div><div class="why">{esc(why)}</div></div></div>
      <div class="body">{esc(body)}</div>
      <div class="pc-next"><div class="lbl2">Next step</div>{esc(d.next_step)}</div>
      <div class="pc-advisory">Advisory only. The copilot cannot purchase, approve spend, change budgets or waive reviews.</div>
    </div>""")
    if result.mode == "rules_fallback":
        st.warning(f"AI assessment unavailable - showing the deterministic policy result. {result.fallback_reason or ''}")


def metrics_row(result: CaseResult) -> None:
    t = result.decision.telemetry
    mode = {"llm": "AI + rules", "rules_only": "Rules only", "rules_fallback": "Rules (AI fallback)"}.get(result.mode, result.mode)
    show(f"""
    <div class="pc-metrics">
      <div class="pc-metric"><div class="k">Latency</div><div class="v">{result.latency_ms / 1000:.1f}s</div><div class="s">{esc(mode)}</div></div>
      <div class="pc-metric"><div class="k">LLM calls</div><div class="v">{t.llm_calls if t else 0}</div><div class="s">{esc(ARCH_SHORT.get(result.architecture, ''))}</div></div>
      <div class="pc-metric"><div class="k">Tool calls</div><div class="v">{t.tool_calls if t else 0}</div><div class="s">incl. policy engine</div></div>
      <div class="pc-metric"><div class="k">Guardrail fixes</div><div class="v">{len(result.guardrails.corrections)}</div><div class="s">{result.guardrails.grounded_claims} AI claims grounded</div></div>
    </div>""")


def approvals_card(result: CaseResult) -> None:
    routes = {r.role: r for r in (result.assessment.approver_routing if result.assessment else [])}
    approvals = result.decision.required_approvals
    specialists = [a for a in approvals if a in SPECIALIST_ROLES]
    business = [a for a in approvals if a not in SPECIALIST_ROLES]

    def row(role: str) -> str:
        who = routes[role].assignee if role in routes else "Assign reviewer"
        return f"<div class='pc-appr'><span class='box'></span><div><div class='role'>{esc(role)}</div><div class='who'>{esc(who)}</div></div></div>"

    blocks = ""
    if specialists:
        blocks += "<div class='pc-group'>1 · Specialist reviews</div>" + "".join(row(a) for a in specialists)
        blocks += "<div class='pc-group'>2 · Business approvals</div>"
    blocks += "".join(row(a) for a in business)
    show(f"<div class='pc-card'><h3>Approvals required · {len(approvals)}</h3>{blocks or '<div class=pc-empty>None</div>'}</div>")


def flags_card(result: CaseResult) -> None:
    flags = sorted(result.decision.risk_flags, key=lambda f: ["critical", "review", "info"].index(FLAG_INFO.get(f, ("info", ""))[0]))
    rows = ""
    for f in flags:
        sev, desc = FLAG_INFO.get(f, ("info", "Additional risk raised by the AI reviewer"))
        color, name = SEVERITY[sev]
        rows += (f"<div class='pc-flag'><span class='pc-sev' style='--c:{color}'>{name}</span>"
                 f"<div><div class='name'>{esc(f).replace('_', '_<wbr>')}</div><div class='desc'>{esc(desc)}</div></div></div>")
    show(f"<div class='pc-card'><h3>Risk flags · {len(flags)}</h3>{rows or '<div class=pc-empty>✓ No risk flags</div>'}</div>")


def list_card(title: str, items: list[str]) -> None:
    if items:
        show(f"<div class='pc-card'><h3>{esc(title)} · {len(items)}</h3><ul class='pc-list'>"
             + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul></div>")


def evidence_panel(result: CaseResult) -> None:
    groups: dict[str, list] = {}
    for e in result.decision.evidence:
        groups.setdefault(e.source.replace(" (agent)", ""), []).append(e)
    out = ""
    for key, title, icon in EVIDENCE_GROUPS:
        items = groups.pop(key, [])
        if not items:
            continue
        rows = ""
        for e in items:
            ai = e.source.endswith("(agent)")
            rows += (f"<div class='pc-ev{' ai' if ai else ''}'>{esc(e.finding)}{'<span class=pc-ai>AI</span>' if ai else ''}"
                     f"<div class='ref'>{esc(e.reference or key)}</div></div>")
        out += f"<div class='pc-evgroup'><div class='pc-evhead'>{icon} {title}<span class='cnt'>{len(items)}</span></div>{rows}</div>"
    show(f"<div class='pc-card'><h3>Evidence · {len(result.decision.evidence)} findings from tool outputs</h3>{out}"
         "<div class='pc-advisory'>Items marked AI are model interpretations that passed the grounding check against this run's tool outputs.</div></div>")
    if result.guardrails.ungrounded_claims:
        with st.expander(f"⚠️ {len(result.guardrails.ungrounded_claims)} AI claim(s) dropped as ungrounded"):
            st.dataframe(pd.DataFrame(result.guardrails.ungrounded_claims), hide_index=True)


def human_panel(result: CaseResult, request_id: str, architecture: str, request: dict) -> None:
    d = result.decision
    show("<div class='pc-card' style='margin-bottom:8px'><h3>Human decision</h3>"
         "<div class='pc-sub'>Record the reviewer's action. Override and reject need a justification. Every entry goes to the audit log.</div></div>")
    with st.form(f"human_{request_id}_{architecture}", border=True):
        reviewer = st.text_input("Reviewer name", placeholder="e.g. Priya Shah")
        action = st.radio("Action", list(HUMAN_ACTIONS), format_func=HUMAN_ACTIONS.get)
        justification = st.text_area("Justification / notes", placeholder="Required for override or reject", height=90)
        if st.form_submit_button("Record decision", type="primary", width="stretch"):
            try:
                record_human_action(request_id=request_id, reviewer=reviewer, action=action, justification=justification,
                                    architecture=architecture, copilot_recommendation=d.recommendation,
                                    required_approvals=d.required_approvals, risk_flags=d.risk_flags)
                st.success("Decision recorded in the audit log.")
            except ValueError as exc:
                st.error(str(exc))
    c1, c2 = st.columns(2)
    c1.download_button("⬇ Review packet (.md)", approval_packet(result, request), file_name=f"{request_id}_review_packet.md",
                       mime="text/markdown", width="stretch")
    c2.download_button("⬇ Decision (.json)", d.model_dump_json(indent=2), file_name=f"{request_id}_decision.json",
                       mime="application/json", width="stretch")


def trace_panel(result: CaseResult) -> None:
    g, a = result.guardrails, result.assessment
    with st.expander("🔍 Audit trace: policy rules, approver routing, tool calls, guardrails, agent transcript"):
        if a:
            st.markdown("**Policy rules applied (deterministic, section-cited)**")
            st.dataframe(pd.DataFrame([{"section": h.section, "rule": h.rule, "finding": h.finding,
                                        "adds approvals": ", ".join(h.adds_approvals), "adds flags": ", ".join(h.adds_flags)}
                                       for h in a.rule_hits]), hide_index=True, width="stretch")
            st.markdown("**Approver routing**")
            st.dataframe(pd.DataFrame([r.model_dump() for r in a.approver_routing]), hide_index=True, width="stretch")
        st.markdown("**Tool calls**")
        st.dataframe(pd.DataFrame([{"tool": c.name, "caller": c.caller, "ok": c.ok, "cached": c.cached,
                                    "latency_ms": c.latency_ms, "args": json.dumps(c.args)} for c in result.tool_log]),
                     hide_index=True, width="stretch")
        st.markdown("**Guardrails**")
        st.json(g.to_dict(), expanded=False)
        if result.evidence_pack:
            st.markdown("**Stage-1 evidence pack (Analyst → Reviewer handoff)**")
            st.json(result.evidence_pack, expanded=False)
        for r in result.agent_runs:
            st.markdown(f"**Agent transcript · {r.role}** · {r.llm_calls} LLM calls · {r.usage.get('input_tokens', 0)} in / "
                        f"{r.usage.get('output_tokens', 0)} out tokens")
            st.json(r.transcript, expanded=False)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### Analyze a request")
    requests_by_id = {r["request_id"]: r for r in repo.requests}

    def _fmt(rid: str) -> str:
        r = requests_by_id[rid]
        cost = r.get("annual_cost_usd")
        return f"{rid} · {r.get('product_name') or '(no product)'}" + (f" · ${cost:,.0f}" if cost is not None else "")

    request_id = st.selectbox("Purchase request", list(requests_by_id), format_func=_fmt)
    architecture = st.radio("Architecture", list(ARCH_LABELS), format_func=ARCH_LABELS.get, index=0,
                            help="A and B use the LLM. Rules only is the deterministic baseline (instant, no model calls).")
    run_clicked = st.button("Analyze request", type="primary", width="stretch")
    st.caption("Recommendations are advisory. Every approval stays with a human.")
    st.divider()

    with st.expander("➕ Submit a new request"):
        with st.form("new_request", clear_on_submit=False, border=False):
            emp_ids = [e["employee_id"] for e in repo.employees]
            f_req = st.selectbox("Requester", emp_ids, format_func=lambda e: f"{e} · {repo.get_employee(e)['name']}")
            f_product = st.text_input("Product name")
            f_vendor = st.text_input("Vendor name")
            f_category = st.text_input("Category", placeholder="e.g. Analytics / BI")
            f_cost = st.text_input("Annual cost (USD)", placeholder="leave blank if unknown")
            f_users = st.text_input("Number of users", placeholder="leave blank if unknown")
            f_data = st.selectbox("Data access level", ["unknown", "none", "internal_documents", "internal_marketing",
                                                        "confidential_documents", "source_code", "production_telemetry",
                                                        "employee_pii", "customer_pii", "credentials"])
            f_integ = st.text_input("Integrations (comma separated)", placeholder="e.g. SSO, CRM")
            f_just = st.text_area("Business justification")
            f_urgency = st.selectbox("Urgency", ["normal", "high", "urgent"])
            submitted = st.form_submit_button("Add & analyze", type="primary", width="stretch")
        if submitted:
            def _num(x: str):
                try:
                    return float(x.replace(",", "").replace("$", "")) if x.strip() else None
                except ValueError:
                    return None
            new_id = f"NEW-{len([r for r in repo.requests if r['request_id'].startswith('NEW-')]) + 1:03d}"
            users = _num(f_users)
            repo.upsert_request({
                "request_id": new_id, "requester_id": f_req, "product_name": f_product.strip(), "vendor_name": f_vendor.strip(),
                "category": f_category.strip(), "annual_cost_usd": _num(f_cost), "user_count": int(users) if users else None,
                "business_justification": f_just.strip(), "data_access_level": f_data,
                "requested_integrations": [x.strip() for x in f_integ.split(",") if x.strip()], "urgency": f_urgency,
            })
            st.session_state.pending = (new_id, architecture)
            st.rerun()

if st.session_state.get("pending"):
    request_id, architecture = st.session_state.pop("pending")
    run_clicked = True

request = repo.get_request(request_id) or {}
hero(is_healthy(settings.vendor_risk_base_url))

tab_copilot, tab_compare, tab_eval, tab_audit, tab_how = st.tabs(
    ["🧾 Copilot", "⚖️ Compare architectures", "📊 Evaluation", "📜 Audit log", "ℹ️ How it works"])

# ---------------------------------------------------------------------------
# Copilot tab
# ---------------------------------------------------------------------------
with tab_copilot:
    key = (request_id, architecture, json.dumps(request, sort_keys=True, default=str))
    steps = st.empty()
    if run_clicked:
        steps.markdown(stepper(2, active=3), unsafe_allow_html=True)
        with st.spinner(f"Gathering evidence and applying policy with {ARCH_LABELS[architecture]} ..."):
            st.session_state.results[key] = run_case(request_id, architecture)
    result: CaseResult | None = st.session_state.results.get(key)
    steps.markdown(stepper(4, human=True) if result else stepper(1, active=2), unsafe_allow_html=True)

    left, right = st.columns([0.92, 1.08], gap="large")
    with left:
        request_card(request, result)
        if result:
            list_card("Missing information", result.decision.missing_information)
            if result.assessment:
                list_card("Open items for reviewers", result.assessment.open_items)
    with right:
        if result is None:
            show("""
            <div class="pc-card" style="text-align:center;padding:42px 24px">
              <div style="font-size:2.4rem">🧭</div>
              <div class="pc-title" style="margin-top:6px">Ready to analyze</div>
              <div class="pc-sub" style="max-width:420px;margin:6px auto 0">Choose an architecture in the sidebar and click
              <b>Analyze request</b>. The copilot calls its tools, runs the deterministic policy engine and prepares a
              recommendation for human review.</div>
            </div>""")
        else:
            decision_card(result)
            metrics_row(result)
            st.write("")
            c1, c2 = st.columns(2, gap="medium")
            with c1:
                approvals_card(result)
            with c2:
                flags_card(result)

    if result is not None:
        ev_col, act_col = st.columns([1.3, 0.7], gap="large")
        with ev_col:
            evidence_panel(result)
        with act_col:
            human_panel(result, request_id, architecture, request)
        trace_panel(result)

# ---------------------------------------------------------------------------
# Compare tab
# ---------------------------------------------------------------------------
with tab_compare:
    show(f"<div class='pc-card'><h3>Same request, three ways · {esc(request_id)}</h3>"
         "<div class='pc-sub'>Runs Architecture A, Architecture B and the rules-only baseline on the selected request in "
         "parallel. Differences are highlighted.</div></div>")
    if st.button("Run comparison", type="primary"):
        with st.spinner("Running all architectures ..."):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {a: pool.submit(run_case, request_id, a) for a in ARCH_LABELS}
                st.session_state.compare = (request_id, {a: f.result() for a, f in futures.items()})
    comp = st.session_state.get("compare")
    if comp and comp[0] == request_id:
        res = comp[1]
        statuses = {r.guardrails.final_status for r in res.values()}
        all_appr = [set(r.decision.required_approvals) for r in res.values()]
        all_flags = [set(r.decision.risk_flags) - {"ai_assessment_unavailable"} for r in res.values()]
        cols = st.columns(3, gap="medium")
        for col, (arch, r) in zip(cols, res.items()):
            color, icon, _ = STATUS_META.get(r.guardrails.final_status or "", ("#64748b", "•", ""))
            t = r.decision.telemetry
            appr = set(r.decision.required_approvals)
            flags = set(r.decision.risk_flags) - {"ai_assessment_unavailable"}
            appr_diff = "<span class='pc-diff'>differs</span>" if any(appr != o for o in all_appr) else ""
            flag_diff = "<span class='pc-diff'>differs</span>" if any(flags != o for o in all_flags) else ""
            st_diff = "<span class='pc-diff'>differs</span>" if len(statuses) > 1 else ""
            with col:
                show(f"""
                <div class="pc-cmp" style="--c:{color}">
                  <div class="arch">{esc(ARCH_LABELS[arch])}</div>
                  <div class="st">{icon} {esc(STATUS_LABELS.get(r.guardrails.final_status or '', ''))}{st_diff}</div>
                  <div class="pc-kv"><b>Approvals</b>{appr_diff}<br>{esc(', '.join(r.decision.required_approvals))}</div>
                  <div class="pc-kv" style="margin-top:6px"><b>Risk flags</b>{flag_diff}<br>{esc(', '.join(sorted(flags)) or 'none')}</div>
                  <div class="pc-kv" style="margin-top:6px"><b>Run</b><br>{r.latency_ms / 1000:.1f}s · {t.llm_calls} LLM · {t.tool_calls} tools · {esc(r.mode)}</div>
                  <div class="pc-kv" style="margin-top:6px"><b>Guardrail corrections</b> {len(r.guardrails.corrections)}</div>
                  <div class="pc-next" style="margin-top:10px"><div class="lbl2">Next step</div>{esc(r.decision.next_step)}</div>
                </div>""")
    elif not settings.llm_enabled:
        st.info("No LLM key is configured, so A and B run in deterministic fallback mode and will match the baseline.")

# ---------------------------------------------------------------------------
# Evaluation tab
# ---------------------------------------------------------------------------
with tab_eval:
    summary_path = RESULTS / "summary.json"
    if summary_path.exists():
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        meta, summary = data["meta"], data["summary"]
        cmp_md = (RESULTS / "comparison.md").read_text(encoding="utf-8") if (RESULTS / "comparison.md").exists() else ""
        decision_line = next((ln for ln in cmp_md.splitlines() if "decision rule" in ln.lower()), "")
        show(f"<div class='pc-win'><div class='t'>🏁 {esc(decision_line.replace('**', '').strip()) or 'Evaluation results'}</div>"
             f"<div class='d'>{meta['cases']} cases × {meta['repeats']} repeat(s) · same test set for every architecture · "
             f"model {esc(meta.get('model') or 'n/a')} · generated {esc(meta['generated_at'])}</div></div>")
        archs = [a for a in ("single", "staged", "rules") if a in summary]
        best = max(archs, key=lambda a: summary[a].get("pass_rate") or 0)
        cols = st.columns(len(archs), gap="medium")
        for col, a in zip(cols, archs):
            s = summary[a]

            def fmt(v, unit=""):
                return "n/a" if v is None else f"{v}{unit}"
            with col:
                show(f"""
                <div class="pc-archcard {'best' if a == best else ''}">
                  <div class="name">{esc(ARCH_LABELS[a])}{' · 🏆' if a == best else ''}</div>
                  <div class="big">{fmt(s.get('pass_rate'), '%')}</div>
                  <div class="pc-sub" style="margin-bottom:8px">cases passing all quality checks</div>
                  <div class="row"><span>Public checks</span><b>{esc(s.get('public_min_checks_pass'))}</b></div>
                  <div class="row"><span>Correct next action</span><b>{fmt(s.get('status_accuracy'), '%')}</b></div>
                  <div class="row"><span>Evidence grounding</span><b>{fmt(s.get('grounding_rate'), '%')}</b></div>
                  <div class="row"><span>Avg latency</span><b>{(s.get('avg_latency_ms') or 0) / 1000:.1f}s</b></div>
                  <div class="row"><span>Avg LLM / tool calls</span><b>{fmt(s.get('avg_llm_calls'))} / {fmt(s.get('avg_tool_calls'))}</b></div>
                  <div class="row"><span>Failed cases</span><b>{esc(', '.join(s.get('failed_cases') or []) or 'none')}</b></div>
                </div>""")
        st.write("")
        frames = [pd.read_csv(RESULTS / f"runs_{a}.csv") for a in archs if (RESULTS / f"runs_{a}.csv").exists()]
        if frames:
            df = pd.concat(frames)
            st.markdown("##### Per-case results")
            pivot = df.pivot_table(index=["case_id", "edge_case"], columns="architecture", values="passed", aggfunc="mean")
            pivot = pivot[[a for a in archs if a in pivot.columns]].rename(columns=ARCH_SHORT)
            st.dataframe(pivot.map(lambda v: "✅ pass" if v == 1 else ("❌ fail" if v == 0 else f"{v:.0%}")), width="stretch",
                         height=36 * (len(pivot) + 1) + 4)
        with st.expander("Full metrics table"):
            st.markdown(cmp_md.split("\n", 2)[-1] if cmp_md else "No comparison file.")
    else:
        st.info("No results yet. Run `python evals/run_eval_suite.py` (add `--update-docs` to refresh the README).")

# ---------------------------------------------------------------------------
# Audit log tab
# ---------------------------------------------------------------------------
with tab_audit:
    show("<div class='pc-card'><h3>Reviewer decisions</h3><div class='pc-sub'>Append-only log of human actions. The copilot "
         "itself never approves anything.</div></div>")
    log = read_log()
    if log:
        st.dataframe(pd.DataFrame(log)[["timestamp_utc", "request_id", "reviewer", "action", "justification",
                                        "copilot_recommendation", "architecture"]].iloc[::-1], hide_index=True, width="stretch")
    else:
        st.info("No reviewer decisions recorded yet. Analyze a request and use the Human decision panel.")

# ---------------------------------------------------------------------------
# How it works
# ---------------------------------------------------------------------------
with tab_how:
    show("""
    <div class="pc-card"><h3>Design principle</h3>
    <div class="pc-tiles">
      <div class="pc-tile"><div class="k">🤖 AI</div><div class="v">Interprets context & recommends</div>
      <div class="pc-sub">Is the stated gap credible? Does the text reveal data the form didn't declare? What should we ask?</div></div>
      <div class="pc-tile"><div class="k">⚙️ Code</div><div class="v">Thresholds & deterministic checks</div>
      <div class="pc-sub">Approval tiers, budget, 365-day review currency, registry/API conflicts, review triggers.</div></div>
      <div class="pc-tile"><div class="k">👤 Human</div><div class="v">Sensitive approvals & exceptions</div>
      <div class="pc-sub">Every approval, budget exception and override, logged with justification.</div></div>
    </div></div>""")
    st.graphviz_chart("""
digraph G { rankdir=LR; bgcolor="transparent"; node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, color="#cbd5e1", fillcolor="#f8fafc"];
  edge [color="#94a3b8"];
  req [label="Purchase request\\n(untrusted text)", fillcolor="#e0e7ff"];
  subgraph cluster_tools { label="Tools (8)"; style="rounded,dashed"; color="#cbd5e1"; fontname="Helvetica";
    t1 [label="request + injection scan"]; t2 [label="requester profile"]; t3 [label="budget check (det.)"];
    t4 [label="catalog search (det.)"]; t5 [label="vendor registry"]; t6 [label="vendor-risk API"]; t8 [label="policy lookup"]; }
  agent [label="LLM agent(s)\\nA: single agent\\nB: analyst -> reviewer", fillcolor="#ede9fe"];
  engine [label="Deterministic policy engine\\nthresholds, budget, review dates,\\nconflicts, triggers", fillcolor="#fef3c7"];
  guard [label="Guardrails\\nescalate, never de-escalate\\ngrounding + language checks", fillcolor="#fee2e2"];
  out [label="ProcurementDecision\\n+ evidence", fillcolor="#dcfce7"];
  human [label="Human reviewers\\nManager / DH / Finance / CFO /\\nSecurity / Privacy / Legal", fillcolor="#fde68a"];
  req -> agent; agent -> {t1 t2 t3 t4 t5 t6 t8}; t1 -> engine; t3 -> engine; t4 -> engine; t5 -> engine; t6 -> engine;
  agent -> guard; engine -> guard; guard -> out -> human;
}""", width="stretch")
