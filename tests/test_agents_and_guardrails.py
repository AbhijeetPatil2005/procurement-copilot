from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from src.copilot.fakes import in_process_fetcher
from src.copilot.llm import LLMUnavailable, ScriptedSession
from src.copilot.pipeline import analyze
from tests.helpers import adversarial_single, cooperative_single, cooperative_staged, turn


def run(request_id: str, architecture: str, factory):
    return analyze(request_id, architecture=architecture, vendor_fetcher=in_process_fetcher(), session_factory=factory)


class SingleAgentTests(unittest.TestCase):
    def test_cooperative_agent_produces_compliant_decision(self):
        res = run("REQ-1002", "single", lambda s, u, t: cooperative_single("REQ-1002"))
        d = res.decision
        self.assertEqual(res.mode, "llm")
        self.assertTrue({"Department Head", "Finance", "Procurement", "Security", "Legal"} <= set(d.required_approvals))
        self.assertTrue(res.guardrails.raw_policy_compliant)
        self.assertEqual(d.telemetry.llm_calls, 4)
        self.assertGreaterEqual(d.telemetry.tool_calls, 6)
        self.assertTrue(d.human_review_required)
        self.assertTrue(any(e.source.endswith("(agent)") for e in d.evidence))

    def test_guardrails_neutralise_a_compromised_agent(self):
        res = run("REQ-1006", "single", lambda s, u, t: adversarial_single("REQ-1006"))
        d, g = res.decision, res.guardrails
        self.assertEqual(g.final_status, "REQUEST_CLARIFICATION")
        self.assertTrue(g.status_overridden)
        self.assertTrue(g.unsafe_language_blocked)
        self.assertIn("prompt_injection_detected", d.risk_flags)
        self.assertIn("missing_information", d.risk_flags)
        self.assertNotIn("CFO", d.required_approvals)
        self.assertEqual(len(g.ungrounded_claims), 2)           # $999,999 and $123,456 were invented
        self.assertNotIn("approved by the CFO", d.recommendation)
        self.assertFalse(g.raw_policy_compliant)

    def test_llm_cannot_remove_engine_approvals(self):
        factory = lambda s, u, t: cooperative_single("REQ-1005", overrides={"required_approvals": ["Department Head"], "risk_flags": []})
        res = run("REQ-1005", "single", factory)
        self.assertTrue({"Finance", "Security", "Privacy", "Legal"} <= set(res.decision.required_approvals))
        self.assertIn("budget_insufficient", res.decision.risk_flags)
        self.assertIn("Finance", res.guardrails.omitted_approvals)

    def test_llm_can_escalate_with_added_review(self):
        factory = lambda s, u, t: cooperative_single("REQ-1001", overrides={
            "status": "ESCALATE_SPECIALIST_REVIEW", "required_approvals": ["Manager", "Privacy"],
            "risk_flags": ["privacy_review_required"]})
        res = run("REQ-1001", "single", factory)
        self.assertEqual(res.guardrails.final_status, "ESCALATE_SPECIALIST_REVIEW")
        self.assertIn("Privacy", res.decision.required_approvals)

    def test_unbacked_escalation_is_reverted(self):
        factory = lambda s, u, t: cooperative_single("REQ-1001", overrides={"status": "ESCALATE_SPECIALIST_REVIEW"})
        res = run("REQ-1001", "single", factory)
        self.assertEqual(res.guardrails.final_status, "ROUTE_FOR_APPROVAL")

    def test_invalid_submission_gets_a_retry(self):
        bad = turn(SimpleNamespace(id="x1", name="submit_decision", input={"status": "APPROVE"}))
        inner = cooperative_single("REQ-1001")
        steps = [lambda s: bad] + inner.steps
        res = run("REQ-1001", "single", lambda s, u, t: ScriptedSession(steps))
        self.assertEqual(res.mode, "llm")
        self.assertEqual(res.agent_runs[0].validation_errors, 1)

    def test_provider_failure_falls_back_to_rules(self):
        def boom(sess):
            raise LLMUnavailable("network down")
        res = run("REQ-1009", "single", lambda s, u, t: ScriptedSession([boom]))
        self.assertEqual(res.mode, "rules_fallback")
        self.assertIn("ai_assessment_unavailable", res.decision.risk_flags)
        self.assertIn("vendor_risk_unavailable", res.decision.risk_flags)
        self.assertEqual(res.guardrails.final_status, "MANUAL_REVIEW_REQUIRED")

    def test_agent_that_never_submits_falls_back(self):
        res = run("REQ-1001", "single", lambda s, u, t: ScriptedSession([lambda s: turn(text="hmm")] * 3))
        self.assertEqual(res.mode, "rules_fallback")
        self.assertIn("AgentIncomplete", res.fallback_reason)
        self.assertEqual(res.decision.telemetry.llm_calls, 2)  # failed attempt still counted

    def test_unknown_tool_is_rejected_not_executed(self):
        steps = [lambda s: turn(SimpleNamespace(id="u1", name="approve_purchase", input={}))] + cooperative_single("REQ-1001").steps
        res = run("REQ-1001", "single", lambda s, u, t: ScriptedSession(steps))
        self.assertNotIn("approve_purchase", res.decision.telemetry.tool_names)


class StagedTests(unittest.TestCase):
    def test_staged_pipeline(self):
        res = run("REQ-1001", "staged", cooperative_staged("REQ-1001"))
        self.assertEqual(res.mode, "llm")
        self.assertEqual([r.role for r in res.agent_runs], ["analyst", "reviewer"])
        self.assertEqual(res.decision.required_approvals, ["Manager"])
        self.assertIsNotNone(res.evidence_pack)
        self.assertEqual(res.decision.telemetry.llm_calls, 4)
        self.assertIn("evaluate_procurement_policy", res.decision.telemetry.tool_names)

    def test_rules_architecture_uses_no_llm(self):
        res = analyze("REQ-1003", architecture="rules", vendor_fetcher=in_process_fetcher())
        self.assertEqual(res.decision.telemetry.llm_calls, 0)
        self.assertEqual(res.mode, "rules_only")

    def test_unknown_request(self):
        res = analyze("REQ-404", architecture="rules", vendor_fetcher=in_process_fetcher())
        self.assertIn("missing_information", res.decision.risk_flags)
        self.assertTrue(res.decision.human_review_required)

    def test_trace_is_json_serialisable(self):
        res = run("REQ-1001", "staged", cooperative_staged("REQ-1001"))
        json.dumps(res.trace(), default=str)


if __name__ == "__main__":
    unittest.main()
