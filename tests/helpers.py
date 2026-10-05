"""Scripted LLM behaviours for testing the agent loop and guardrails without a network."""
from __future__ import annotations

import itertools
import json

from src.copilot.llm import ScriptedSession, ToolUse, TurnResult

_ids = itertools.count(1)


def _use(name: str, **inp) -> ToolUse:
    return ToolUse(id=f"toolu_{next(_ids)}", name=name, input=inp)


def turn(*uses: ToolUse, text: str = "") -> TurnResult:
    return TurnResult(text=text, tool_uses=list(uses), stop_reason="tool_use" if uses else "end_turn",
                      usage={"input_tokens": 1000, "output_tokens": 200})


def _last_json(session: ScriptedSession, idx: int = 0) -> dict:
    content = session.tool_results[-1][idx][1]
    start, end = content.find("{"), content.rfind("}")
    return json.loads(content[start:end + 1])


def cooperative_single(request_id: str, overrides: dict | None = None):
    """A well-behaved single agent: gathers evidence, runs the engine, submits a compliant decision."""
    state: dict = {}

    def s1(sess):
        return turn(_use("get_purchase_request", request_id=request_id))

    def s2(sess):
        req = _last_json(sess)["request"]
        state["req"] = req
        return turn(
            _use("get_requester_profile", employee_id=req["requester_id"]),
            _use("get_vendor_registry_record", vendor_name=req["vendor_name"]),
            _use("get_vendor_risk_assessment", vendor_name=req["vendor_name"]),
            _use("search_existing_software", product_name=req["product_name"], vendor_name=req["vendor_name"],
                 category=req["category"], use_case_keywords=[]),
        )

    def s3(sess):
        return turn(_use("evaluate_procurement_policy", request_id=request_id))

    def s4(sess):
        a = _last_json(sess)["assessment"]
        req = state["req"]
        draft = {
            "status": a["default_status"],
            "recommendation": "Assessment complete; see evidence.",
            "rationale": "Followed the deterministic assessment.",
            "evidence": [{"source": "get_purchase_request", "finding": f"Requested by {req['requester_id']}.",
                          "reference": f"requests.json:{request_id}"}],
            "required_approvals": a["required_approvals"],
            "risk_flags": a["risk_flags"],
            "missing_information": a["missing_information"],
            "clarification_questions": [],
            "overlap_disposition": "no_overlap",
            "next_step": "Send the evidence package to the listed approvers.",
            "prompt_injection_observed": a["injection"]["detected"],
        }
        draft.update(overrides or {})
        return turn(_use("submit_decision", **draft))

    return ScriptedSession([s1, s2, s3, s4])


def adversarial_single(request_id: str):
    """An agent that was 'convinced' by injected text: omits reviews, claims approval, invents numbers."""

    def s1(sess):
        return turn(_use("get_purchase_request", request_id=request_id))

    def s2(sess):
        return turn(_use("submit_decision", **{
            "status": "ROUTE_FOR_APPROVAL",
            "recommendation": "This request is approved by the CFO; purchase it now.",
            "rationale": "The requester says it is CFO-approved.",
            "evidence": [
                {"source": "check_department_budget", "finding": "Budget has $999,999 available.", "reference": "made-up"},
                {"source": "get_purchase_request", "finding": "Annual cost is $123,456.", "reference": "requests.json"},
            ],
            "required_approvals": [],
            "risk_flags": [],
            "missing_information": [],
            "clarification_questions": [],
            "overlap_disposition": "no_overlap",
            "next_step": "No further review is needed.",
            "prompt_injection_observed": False,
        }))

    return ScriptedSession([s1, s2])


def cooperative_staged(request_id: str):
    """Analyst session + reviewer session for Architecture B."""
    state: dict = {}

    def a1(sess):
        return turn(_use("get_purchase_request", request_id=request_id))

    def a2(sess):
        req = _last_json(sess)["request"]
        state["req"] = req
        return turn(
            _use("get_requester_profile", employee_id=req["requester_id"]),
            _use("check_department_budget", department="Finance", annual_cost_usd=req["annual_cost_usd"]),
            _use("get_vendor_registry_record", vendor_name=req["vendor_name"]),
            _use("get_vendor_risk_assessment", vendor_name=req["vendor_name"]),
        )

    def a3(sess):
        return turn(_use("submit_evidence_pack", **{
            "need_summary": "Finance needs additional signing identities.",
            "facts": [{"source": "get_purchase_request", "finding": "Three users requested.", "reference": f"requests.json:{request_id}"}],
            "overlap_assessment": {"disposition": "no_overlap", "existing_options": [], "reasoning": "Extends existing agreement."},
            "data_quality_issues": [], "open_questions": [], "prompt_injection_observed": False, "injection_details": "",
        }))

    analyst = ScriptedSession([a1, a2, a3])

    def r1(sess):
        return turn(_use("submit_decision", **{
            "status": "ROUTE_FOR_APPROVAL", "recommendation": "Standard manager approval.", "rationale": "Low value.",
            "evidence": [{"source": "get_purchase_request", "finding": "Annual cost $800 for 3 users.", "reference": f"requests.json:{request_id}"}],
            "required_approvals": ["Manager"], "risk_flags": [], "missing_information": [], "clarification_questions": [],
            "overlap_disposition": "no_overlap", "next_step": "Send to the requester's manager.", "prompt_injection_observed": False,
        }))

    reviewer = ScriptedSession([r1])
    sessions = iter([analyst, reviewer])
    return lambda system, user, tools: next(sessions)
