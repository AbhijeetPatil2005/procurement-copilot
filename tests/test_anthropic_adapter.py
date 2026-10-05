"""Exercises AnthropicSession against a fake client that returns real `anthropic` SDK types."""
from __future__ import annotations

import unittest

import anthropic
from anthropic.types import Message

from src.config import get_settings
from src.copilot.fakes import in_process_fetcher
from src.copilot.llm import AnthropicSession, LLMUnavailable
from src.copilot.pipeline import analyze


def _message(content: list[dict], stop_reason: str) -> Message:
    return Message.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 150, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0},
    })


class _FakeMessages:
    def __init__(self, scripted: list, log: list):
        self.scripted, self.log = scripted, log

    def create(self, **params):
        self.log.append(params)
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, scripted: list):
        self.log: list[dict] = []
        self.messages = _FakeMessages(scripted, self.log)
        self.beta = type("B", (), {"messages": self.messages})()


def _decision_input() -> dict:
    return {
        "status": "ROUTE_FOR_APPROVAL", "recommendation": "Manager approval only.", "rationale": "Low value, approved vendor.",
        "evidence": [{"source": "get_purchase_request", "finding": "Annual cost $800 for 3 users.", "reference": "requests.json:REQ-1001"}],
        "required_approvals": ["Manager"], "risk_flags": [], "missing_information": [], "clarification_questions": [],
        "overlap_disposition": "no_overlap", "next_step": "Send to the requester's manager.", "prompt_injection_observed": False,
    }


class AnthropicAdapterTests(unittest.TestCase):
    def _settings(self):
        s = get_settings()
        return s.__class__(**{**s.__dict__, "llm_provider": "anthropic", "model_name": "claude-opus-5-5", "anthropic_fallbacks": True})

    def test_tool_loop_request_shape_and_history(self):
        client = _FakeClient([
            _message([{"type": "thinking", "thinking": "", "signature": "sig"},
                      {"type": "tool_use", "id": "toolu_1", "name": "get_purchase_request", "input": {"request_id": "REQ-1001"}}], "tool_use"),
            _message([{"type": "tool_use", "id": "toolu_2", "name": "submit_decision", "input": _decision_input()}], "tool_use"),
        ])
        settings = self._settings()
        factory = lambda system, user, tools: AnthropicSession(settings, system, user, tools, client=client)
        res = analyze("REQ-1001", architecture="single", vendor_fetcher=in_process_fetcher(), session_factory=factory)

        self.assertEqual(res.mode, "llm")
        self.assertEqual(res.decision.required_approvals, ["Manager"])
        first, second = client.log
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual(first["output_config"], {"effort": settings.llm_effort})
        self.assertEqual(first["cache_control"], {"type": "ephemeral"})
        self.assertEqual(first["fallbacks"], "default")
        self.assertTrue(all(t.get("strict") for t in first["tools"]))
        self.assertTrue(all(t["input_schema"].get("additionalProperties") is False for t in first["tools"]))
        # second request carries the full assistant content (incl. thinking) and one user message with all tool results
        assistant, tool_msg = second["messages"][1], second["messages"][2]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual([b.type for b in assistant["content"]], ["thinking", "tool_use"])
        self.assertEqual(tool_msg["content"][0]["type"], "tool_result")
        self.assertEqual(tool_msg["content"][0]["tool_use_id"], "toolu_1")
        self.assertIn("untrusted_business_data", tool_msg["content"][0]["content"])
        self.assertEqual(res.usage["cache_read_input_tokens"], 1800)

    def test_refusal_falls_back(self):
        client = _FakeClient([_message([], "refusal")])
        settings = self._settings()
        factory = lambda system, user, tools: AnthropicSession(settings, system, user, tools, client=client)
        res = analyze("REQ-1001", architecture="single", vendor_fetcher=in_process_fetcher(), session_factory=factory)
        self.assertEqual(res.mode, "rules_fallback")
        self.assertIn("refusal", res.fallback_reason)

    def test_bad_request_degrades_optional_features_once(self):
        import httpx
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        err = anthropic.BadRequestError("strict not supported", response=httpx.Response(400, request=req), body=None)
        client = _FakeClient([err, _message([{"type": "text", "text": "ok"}], "end_turn")])
        s = AnthropicSession(self._settings(), "sys", "hi", [], client=client)
        s.send()
        self.assertNotIn("fallbacks", client.log[1])
        self.assertFalse(s.use_strict)

    def test_auth_error_is_llm_unavailable(self):
        import httpx
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        err = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=req), body=None)
        s = AnthropicSession(self._settings(), "sys", "hi", [], client=_FakeClient([err]))
        with self.assertRaises(LLMUnavailable):
            s.send()


if __name__ == "__main__":
    unittest.main()
