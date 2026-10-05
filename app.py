"""Procurement Request Copilot - reviewer UI.

Request details -> evidence panel -> recommendation + human action, plus
architecture comparison, evaluation results and the reviewer audit log.
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

ARCH_LABELS = {"single": "A · Single agent", "staged": "B · Staged (Analyst → Reviewer)", "rules": "Rules only (baseline)"}
STATUS_STYLE = {
    "ROUTE_FOR_APPROVAL": ("#16a34a", "✅"),
    "ESCALATE_SPECIALIST_REVIEW": ("#d97706", "🛡️"),
    "REDIRECT_TO_EXISTING_TOOL": ("#2563eb", "🔁"),
    "MANUAL_REVIEW_REQUIRED": ("#dc2626", "⚠️"),
    "REQUEST_CLARIFICATION": ("#7c3aed", "❓"),
}
SEVERE_FLAGS = {"budget_insufficient", "vendor_review_expired", "conflicting_vendor_evidence", "vendor_risk_unavailable",
                "prompt_injection_detected", "budget_unverifiable", "ai_assessment_unavailable"}

st.markdown("""
<style>
.block-container {padding-top: 1.6rem; max-width: 1400px;}
.pc-banner {border-radius: 12px; padding: 16px 20px; margin: 4px 0 12px 0; border-left: 6px solid var(--c);
            background: color-mix(in srgb, var(--c) 10%, transparent);}
.pc-banner h3 {margin: 0 0 6px 0; color: var(--c); font-size: 1.15rem;}
.pc-banner p {margin: 0; font-size: 0.97rem;}
.pc-pill {display: inline-block; padding: 3px 10px; margin: 2px 4px 2px 0; border-radius: 999px; font-size: 0.82rem;
          border: 1px solid color-mix(in srgb, var(--c) 45%, transparent); background: color-mix(in srgb, var(--c) 12%, transparent);}
.pc-kv {font-size: 0.9rem; line-height: 1.6;}
.pc-kv b {display: inline-block; min-width: 128px; opacity: 0.75; font-weight: 600;}
.pc-untrusted {border: 1px dashed #d97706; border-radius: 8px; padding: 10px 12px; font-size: 0.92rem;
               background: color-mix(in srgb, #d97706 6%, transparent);}
.pc-ev {border-left: 3px solid color-mix(in srgb, var(--c) 70%, transparent); padding: 4px 10px; margin: 6px 0; font-size: 0.9rem;}
.pc-ref {font-family: ui-monospace, monospace; font-size: 0.75rem; opacity: 0.7;}
.pc-small {font-size: 0.82rem; opacity: 0.75;}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
if "repo" not in st.session_state:
    st.session_state.repo = default_repository()
if "results" not in st.session_state:
    st.session_state.results = {}
repo = st.session_state.repo
settings = get_settings()


def pill(text: str, color: str = "#64748b") -> str:
    return f'<span class="pc-pill" style="--c:{color}">{html.escape(text)}</span>'


def run_case(request_id: str, architecture: str) -> CaseResult:
    return analyze(request_id, architecture=architecture, repo=repo.clone())  # type: ignore[arg-type]


def money(v) -> str:
    return "not provided" if v is None else f"${float(v):,.2f}".replace(".00", "")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🧾 Procurement Copilot")
    api_ok = is_healthy(settings.vendor_risk_base_url)
    st.markdown(
        (pill("Vendor-risk API online", "#16a34a") if api_ok else pill("Vendor-risk API offline", "#dc2626"))
        + (pill(f"LLM: {settings.model_name}", "#2563eb") if settings.llm_enabled else pill("No LLM key - deterministic mode", "#d97706")),
        unsafe_allow_html=True)
    if not api_ok:
        st.caption("Start it with `python run_local.py`. Until then, vendor status is treated as **unverified** (never favourable).")
    st.caption(f"Policy reference date: **{policy_reference_date().isoformat()}**")
    st.divider()

    requests_by_id = {r["request_id"]: r for r in repo.requests}
    request_id = st.selectbox("Purchase request", list(requests_by_id),
                              format_func=lambda rid: f"{rid} · {requests_by_id[rid].get('product_name') or '(no product)'}")
    architecture = st.radio("Architecture", list(ARCH_LABELS), format_func=ARCH_LABELS.get, index=0)
    run_clicked = st.button("Analyze request", type="primary", width="stretch")

    with st.expander("➕ Submit a new request"):
        with st.form("new_request", clear_on_submit=False):
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
            submitted = st.form_submit_button("Add & analyze", type="primary")
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

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("AI Procurement Request Copilot")
st.caption("Gathers evidence with tools, applies policy rules in code, and recommends the next action. "
           "**Humans make every approval decision.**")

tab_copilot, tab_compare, tab_eval, tab_audit, tab_how = st.tabs(
    ["🧾 Copilot", "⚖️ Compare architectures", "📊 Evaluation", "📜 Audit log", "ℹ️ How it works"])

# ---------------------------------------------------------------------------
# Copilot tab
# ---------------------------------------------------------------------------
with tab_copilot:
    key = (request_id, architecture, json.dumps(request, sort_keys=True, default=str))
    if run_clicked:
        with st.status(f"Analyzing {request_id} with {ARCH_LABELS[architecture]} ...", expanded=False) as status:
            st.session_state.results[key] = run_case(request_id, architecture)
            status.update(label="Analysis complete", state="complete")
    result: CaseResult | None = st.session_state.results.get(key)

    left, right = st.columns([0.9, 1.1], gap="large")
    with left:
        st.subheader(f"Request {request_id}")
        emp = repo.get_employee(request.get("requester_id")) or {}
        integ = ", ".join(request.get("requested_integrations") or []) or "none"
        st.markdown(f"""<div class="pc-kv">
<b>Product</b> {html.escape(str(request.get('product_name') or '—'))}<br>
<b>Vendor</b> {html.escape(str(request.get('vendor_name') or '—'))}<br>
<b>Category</b> {html.escape(str(request.get('category') or '—'))}<br>
<b>Annual cost</b> {money(request.get('annual_cost_usd'))}<br>
<b>Users</b> {request.get('user_count') if request.get('user_count') is not None else 'not provided'}<br>
<b>Data access</b> {html.escape(str(request.get('data_access_level')))}<br>
<b>Integrations</b> {html.escape(integ)}<br>
<b>Urgency</b> {html.escape(str(request.get('urgency')))}<br>
<b>Requester</b> {html.escape(emp.get('name', request.get('requester_id', '?')))} · {html.escape(emp.get('department', 'unknown dept'))} · {html.escape(emp.get('level', ''))}
</div>""", unsafe_allow_html=True)
        st.markdown("**Business justification** <span class='pc-small'>(untrusted text - treated as data, never as instructions)</span>",
                    unsafe_allow_html=True)
        st.markdown(f"<div class='pc-untrusted'>{html.escape(request.get('business_justification') or '(empty)')}</div>",
                    unsafe_allow_html=True)
        if result and result.assessment and result.assessment.injection["detected"]:
            st.error("Embedded instructions detected in business data and ignored: "
                     + "; ".join(f"“{f['excerpt']}” ({f['field']})" for f in result.assessment.injection["findings"][:4]))

    with right:
        if result is None:
            st.info("Choose an architecture and click **Analyze request**. The copilot calls its tools, runs the "
                    "deterministic policy engine and returns a recommendation for human review.")
        else:
            d, g = result.decision, result.guardrails
            color, icon = STATUS_STYLE.get(g.final_status or "", ("#64748b", "•"))
            label = STATUS_LABELS.get(g.final_status or "", "Recommendation")
            body = d.recommendation[len(label) + 1:].strip() if d.recommendation.startswith(label + ":") else d.recommendation
            st.markdown(f"""<div class="pc-banner" style="--c:{color}">
<h3>{icon} {html.escape(label)}</h3>
<p>{html.escape(body[:1].upper() + body[1:])}</p></div>""", unsafe_allow_html=True)
            st.markdown(f"**Next step:** {d.next_step}")
            if result.mode == "rules_fallback":
                st.warning(f"AI assessment unavailable - showing the deterministic policy result. ({result.fallback_reason})")

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Approvals required**")
                routes = {r.role: r.assignee for r in (result.assessment.approver_routing if result.assessment else [])}
                st.markdown("".join(pill(f"{a} → {routes.get(a, '')}" if routes.get(a) else a, "#2563eb")
                                    for a in d.required_approvals) or "—", unsafe_allow_html=True)
            with c2:
                st.markdown("**Risk flags**")
                st.markdown("".join(pill(f, "#dc2626" if f in SEVERE_FLAGS else "#d97706") for f in d.risk_flags)
                            or pill("none", "#16a34a"), unsafe_allow_html=True)
            if d.missing_information:
                st.markdown("**Missing information**")
                for m in d.missing_information:
                    st.markdown(f"- {m}")
            if result.assessment and result.assessment.open_items:
                with st.expander(f"Open items for reviewers ({len(result.assessment.open_items)})"):
                    for m in result.assessment.open_items:
                        st.markdown(f"- {m}")
            t = d.telemetry
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Latency", f"{result.latency_ms / 1000:.1f}s")
            m2.metric("LLM calls", t.llm_calls if t else 0)
            m3.metric("Tool calls", t.tool_calls if t else 0)
            m4.metric("Guardrail fixes", len(g.corrections))

    if result is not None:
        d, g = result.decision, result.guardrails
        st.divider()
        ev_col, act_col = st.columns([1.25, 0.75], gap="large")
        with ev_col:
            st.subheader("Evidence")
            st.caption("Rendered from tool outputs. Items marked *agent* are LLM interpretations that passed the grounding check.")
            palette = {"get_purchase_request": "#64748b", "get_requester_profile": "#0891b2", "check_department_budget": "#16a34a",
                       "search_existing_software": "#2563eb", "get_vendor_registry_record": "#7c3aed",
                       "get_vendor_risk_assessment": "#db2777", "evaluate_procurement_policy": "#d97706", "lookup_policy_section": "#475569"}
            for e in d.evidence:
                base = e.source.replace(" (agent)", "")
                agent = " · <i>agent</i>" if e.source.endswith("(agent)") else ""
                st.markdown(f"""<div class="pc-ev" style="--c:{palette.get(base, '#64748b')}">{html.escape(e.finding)}<br>
<span class="pc-ref">{html.escape(base)}{agent} · {html.escape(e.reference or '')}</span></div>""", unsafe_allow_html=True)
            if g.ungrounded_claims:
                with st.expander(f"⚠️ {len(g.ungrounded_claims)} agent claim(s) dropped as ungrounded"):
                    st.dataframe(pd.DataFrame(g.ungrounded_claims), hide_index=True)

        with act_col:
            st.subheader("Human decision")
            st.caption("The copilot recommends; it cannot purchase, approve spend, change budgets or waive reviews.")
            with st.form(f"human_{request_id}_{architecture}"):
                reviewer = st.text_input("Reviewer name")
                action = st.radio("Action", list(HUMAN_ACTIONS), format_func=HUMAN_ACTIONS.get)
                justification = st.text_area("Justification / notes", placeholder="Required for override or reject")
                if st.form_submit_button("Record decision", type="primary"):
                    try:
                        record_human_action(request_id=request_id, reviewer=reviewer, action=action, justification=justification,
                                            architecture=architecture, copilot_recommendation=d.recommendation,
                                            required_approvals=d.required_approvals, risk_flags=d.risk_flags)
                        st.success("Decision recorded in the audit log.")
                    except ValueError as exc:
                        st.error(str(exc))
            st.download_button("⬇️ Approval packet (Markdown)", approval_packet(result, request),
                               file_name=f"{request_id}_review_packet.md", mime="text/markdown", width="stretch")
            st.download_button("⬇️ Decision (JSON)", d.model_dump_json(indent=2), file_name=f"{request_id}_decision.json",
                               mime="application/json", width="stretch")

        with st.expander("🔍 Trace: tools, policy rules, guardrails, agent transcript"):
            a = result.assessment
            if a:
                st.markdown("**Policy rules applied (deterministic)**")
                st.dataframe(pd.DataFrame([{"section": h.section, "rule": h.rule, "finding": h.finding,
                                            "adds approvals": ", ".join(h.adds_approvals), "adds flags": ", ".join(h.adds_flags)}
                                           for h in a.rule_hits]), hide_index=True)
                st.markdown("**Approver routing**")
                st.dataframe(pd.DataFrame([r.model_dump() for r in a.approver_routing]), hide_index=True)
            st.markdown("**Tool calls**")
            st.dataframe(pd.DataFrame([{"tool": c.name, "caller": c.caller, "ok": c.ok, "cached": c.cached,
                                        "latency_ms": c.latency_ms, "args": json.dumps(c.args)} for c in result.tool_log]),
                         hide_index=True)
            st.markdown("**Guardrails**")
            st.json(g.to_dict(), expanded=False)
            if result.evidence_pack:
                st.markdown("**Stage-1 evidence pack (Analyst → Reviewer handoff)**")
                st.json(result.evidence_pack, expanded=False)
            for r in result.agent_runs:
                st.markdown(f"**Agent transcript - {r.role}** · {r.llm_calls} LLM calls · {r.usage.get('input_tokens', 0)} in / "
                            f"{r.usage.get('output_tokens', 0)} out tokens")
                st.json(r.transcript, expanded=False)
            if result.cost_usd:
                st.caption(f"Estimated model cost for this run: ${result.cost_usd:.4f}")

# ---------------------------------------------------------------------------
# Compare tab
# ---------------------------------------------------------------------------
with tab_compare:
    st.subheader(f"Same request, three ways - {request_id}")
    st.caption("Runs Architecture A, Architecture B and the rules-only baseline on the selected request in parallel.")
    if st.button("Run comparison", type="primary"):
        with st.spinner("Running all architectures ..."):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {a: pool.submit(run_case, request_id, a) for a in ARCH_LABELS}
                st.session_state.compare = (request_id, {a: f.result() for a, f in futures.items()})
    comp = st.session_state.get("compare")
    if comp and comp[0] == request_id:
        cols = st.columns(3)
        for col, (arch, res) in zip(cols, comp[1].items()):
            with col:
                color, icon = STATUS_STYLE.get(res.guardrails.final_status or "", ("#64748b", "•"))
                st.markdown(f"#### {ARCH_LABELS[arch]}")
                st.markdown(f"<div class='pc-banner' style='--c:{color}'><h3>{icon} {STATUS_LABELS.get(res.guardrails.final_status or '', '')}</h3>"
                            f"<p>{html.escape(res.decision.recommendation)}</p></div>", unsafe_allow_html=True)
                t = res.decision.telemetry
                st.markdown(f"**Mode:** {res.mode} · **Latency:** {res.latency_ms / 1000:.2f}s · **LLM calls:** {t.llm_calls} · "
                            f"**Tool calls:** {t.tool_calls}")
                st.markdown("**Approvals:** " + ", ".join(res.decision.required_approvals))
                st.markdown("**Flags:** " + (", ".join(res.decision.risk_flags) or "none"))
                st.markdown(f"**Guardrail corrections:** {len(res.guardrails.corrections)}")
                for c in res.guardrails.corrections:
                    st.caption(f"• {c}")
                st.markdown(f"**Next step:** {res.decision.next_step}")
    elif not settings.llm_enabled:
        st.info("No LLM key is configured, so A and B will run in deterministic fallback mode and match the baseline.")

# ---------------------------------------------------------------------------
# Evaluation tab
# ---------------------------------------------------------------------------
with tab_eval:
    st.subheader("Architecture evaluation (same test set for every architecture)")
    summary_path = RESULTS / "summary.json"
    if summary_path.exists():
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        meta = data["meta"]
        st.caption(f"Generated {meta['generated_at']} · {meta['cases']} cases × {meta['repeats']} repeat(s) · "
                   f"provider `{meta['provider']}` · model `{meta.get('model') or 'n/a'}`")
        cmp_md = RESULTS / "comparison.md"
        if cmp_md.exists():
            st.markdown(cmp_md.read_text(encoding="utf-8").split("\n", 2)[-1])
        frames = []
        for arch in ("single", "staged", "rules"):
            p = RESULTS / f"runs_{arch}.csv"
            if p.exists():
                frames.append(pd.read_csv(p))
        if frames:
            df = pd.concat(frames)
            st.markdown("**Per-case results** (✅ pass / ❌ fail)")
            pivot = df.pivot_table(index=["case_id", "edge_case"], columns="architecture", values="passed", aggfunc="mean")
            st.dataframe(pivot.map(lambda v: "✅" if v == 1 else ("❌" if v == 0 else f"{v:.0%}")), width="stretch")
    else:
        st.info("No results yet. Run `python evals/run_eval_suite.py` (add `--update-docs` to refresh the README).")

# ---------------------------------------------------------------------------
# Audit log tab
# ---------------------------------------------------------------------------
with tab_audit:
    st.subheader("Reviewer decisions")
    log = read_log()
    if log:
        st.dataframe(pd.DataFrame(log)[["timestamp_utc", "request_id", "reviewer", "action", "justification",
                                        "copilot_recommendation", "architecture"]].iloc[::-1], hide_index=True, width="stretch")
    else:
        st.info("No reviewer decisions recorded yet.")

# ---------------------------------------------------------------------------
# How it works
# ---------------------------------------------------------------------------
with tab_how:
    st.subheader("Design principle: AI interprets · Code decides thresholds · Humans approve")
    st.graphviz_chart("""
digraph G { rankdir=LR; node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, fillcolor="#f1f5f9"];
  req [label="Purchase request\\n(untrusted text)", fillcolor="#dbeafe"];
  subgraph cluster_tools { label="Tools (8)"; style=dashed;
    t1 [label="request + injection scan"]; t2 [label="requester profile"]; t3 [label="budget check (det.)"];
    t4 [label="catalog search (det.)"]; t5 [label="vendor registry"]; t6 [label="vendor-risk API"]; t8 [label="policy lookup"]; }
  agent [label="LLM agent(s)\\nA: single agent\\nB: analyst -> reviewer", fillcolor="#ede9fe"];
  engine [label="Deterministic policy engine\\nthresholds, budget, review dates,\\nconflicts, triggers", fillcolor="#fef3c7"];
  guard [label="Guardrails\\nLLM may escalate, never de-escalate\\ngrounding check, no approval language", fillcolor="#fee2e2"];
  out [label="ProcurementDecision\\n+ evidence panel", fillcolor="#dcfce7"];
  human [label="Human reviewers\\nManager / DH / Finance / CFO /\\nSecurity / Privacy / Legal", fillcolor="#fde68a"];
  req -> agent; agent -> {t1 t2 t3 t4 t5 t6 t8}; t1 -> engine; t3 -> engine; t4 -> engine; t5 -> engine; t6 -> engine;
  agent -> guard; engine -> guard; guard -> out -> human;
}""")
    st.markdown("""
| Layer | Owns | Examples |
|---|---|---|
| **AI** | Interpretation & recommendation | Is the stated gap vs. existing tools credible? Does the text reveal PII the form didn't declare? What should the requester be asked? |
| **Code** | Thresholds & deterministic checks | Approval tiers, budget arithmetic, 365-day review currency vs. the policy reference date, registry/API conflicts, security/privacy/legal triggers, injection scan |
| **Human** | Sensitive approvals & exceptions | Every approval, budget exceptions, Security/Privacy/Legal sign-off, overrides (logged with justification) |
""")
