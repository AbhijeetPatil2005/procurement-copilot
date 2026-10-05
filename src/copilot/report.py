"""Render a reviewer-ready approval packet (Markdown) from a CaseResult."""
from __future__ import annotations

from src.copilot.pipeline import CaseResult
from src.copilot.policy_engine import STATUS_LABELS


def approval_packet(result: CaseResult, request: dict) -> str:
    d, a = result.decision, result.assessment
    lines = [
        f"# Procurement review packet - {d.request_id}",
        "",
        f"**{request.get('product_name')}** from **{request.get('vendor_name')}** · "
        f"annual cost: {request.get('annual_cost_usd') if request.get('annual_cost_usd') is not None else 'not provided'} USD · "
        f"users: {request.get('user_count') if request.get('user_count') is not None else 'not provided'}",
        "",
        f"> **{STATUS_LABELS.get(result.guardrails.final_status or '', 'Recommendation')}** - {d.recommendation}",
        "",
        f"**Next step:** {d.next_step}",
        "",
        "_This is an advisory recommendation. It does not approve spend; every approval below must be given by a human._",
        "",
        "## Required approvals",
    ]
    routes = {r.role: r for r in (a.approver_routing if a else [])}
    for role in d.required_approvals:
        r = routes.get(role)
        lines.append(f"- [ ] **{role}** - {r.assignee if r else 'assign reviewer'}")
    lines += ["", "## Risk flags", *(f"- `{f}`" for f in d.risk_flags or ["none"])]
    if d.missing_information:
        lines += ["", "## Missing information", *(f"- {m}" for m in d.missing_information)]
    if a and a.open_items:
        lines += ["", "## Open items for reviewers", *(f"- {m}" for m in a.open_items)]
    lines += ["", "## Evidence", "| Source | Finding | Reference |", "|---|---|---|"]
    for e in d.evidence:
        lines.append(f"| {e.source} | {e.finding.replace('|', '/')} | {e.reference or ''} |")
    t = d.telemetry
    lines += ["", "## Run metadata",
              f"- Architecture: {result.architecture} ({result.mode})",
              f"- Model: {result.model or 'none'} · LLM calls: {t.llm_calls if t else 0} · tool calls: {t.tool_calls if t else 0} · "
              f"latency: {result.latency_ms:.0f} ms",
              f"- Policy reference date: {a.reference_date if a else 'n/a'}"]
    if result.guardrails.corrections:
        lines += ["- Guardrail corrections: " + "; ".join(result.guardrails.corrections)]
    return "\n".join(lines) + "\n"
