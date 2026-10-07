"""Web API tests (FastAPI TestClient, deterministic path, no network)."""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient

import web.server as server
from src.copilot.fakes import in_process_fetcher
from src.copilot.pipeline import analyze as real_analyze


def _offline_analyze(request_id, architecture="single", repo=None, **_):
    return real_analyze(request_id, architecture=architecture, repo=repo, vendor_fetcher=in_process_fetcher(), session_factory=False)


class WebApiTests(unittest.TestCase):
    def setUp(self):
        server.STATE.__init__()
        self.patch = mock.patch.object(server, "analyze", _offline_analyze)
        self.patch.start()
        self.audit = mock.patch("src.audit.AUDIT_PATH", server.ROOT / "runtime" / "test_audit_log.jsonl")
        self.client = TestClient(server.app)

    def tearDown(self):
        self.patch.stop()
        (server.ROOT / "runtime" / "test_audit_log.jsonl").unlink(missing_ok=True)

    def test_index_and_static_assets(self):
        self.assertIn("Procurement Copilot", self.client.get("/").text)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/static/styles.css").status_code, 200)

    def test_meta_and_requests(self):
        meta = self.client.get("/api/meta").json()
        self.assertEqual(meta["policy_reference_date"], "2026-09-30")
        reqs = self.client.get("/api/requests").json()
        self.assertEqual(len(reqs), 10)
        self.assertIn("requester_name", reqs[0])

    def test_analyze_returns_full_decision(self):
        r = self.client.post("/api/analyze", json={"request_id": "REQ-1002", "architecture": "rules"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ESCALATE_SPECIALIST_REVIEW")
        self.assertIn("Security", body["decision"]["required_approvals"])
        self.assertTrue(body["decision"]["human_review_required"])
        self.assertTrue(body["assessment"]["approver_routing"])
        listed = {x["request_id"]: x for x in self.client.get("/api/requests").json()}
        self.assertEqual(listed["REQ-1002"]["last_result"]["status"], "ESCALATE_SPECIALIST_REVIEW")

    def test_unknown_request_and_bad_architecture(self):
        self.assertEqual(self.client.post("/api/analyze", json={"request_id": "NOPE", "architecture": "rules"}).status_code, 404)
        self.assertEqual(self.client.post("/api/analyze", json={"request_id": "REQ-1001", "architecture": "magic"}).status_code, 422)

    def test_create_request_does_not_invent_values(self):
        created = self.client.post("/api/requests", json={"requester_id": "E001", "product_name": "Widget", "vendor_name": "WidgetCo",
                                                          "business_justification": "<script>alert(1)</script>"}).json()
        self.assertIsNone(created["annual_cost_usd"])
        res = self.client.post("/api/analyze", json={"request_id": created["request_id"], "architecture": "rules"}).json()
        self.assertEqual(res["status"], "REQUEST_CLARIFICATION")
        self.assertEqual(self.client.post("/api/requests", json={"requester_id": "E999"}).status_code, 400)

    def test_compare_packet_and_decisions(self):
        cmp = self.client.post("/api/compare", json={"request_id": "REQ-1001"}).json()
        self.assertEqual(set(cmp), {"single", "staged", "rules"})
        packet = self.client.get("/api/packet/REQ-1001/rules")
        self.assertEqual(packet.status_code, 200)
        self.assertIn("Required approvals", packet.text)
        with self.audit:
            bad = self.client.post("/api/decisions", json={"request_id": "REQ-1001", "architecture": "rules", "reviewer": "Ann",
                                                           "action": "reject", "justification": ""})
            self.assertEqual(bad.status_code, 400)  # reject needs a justification
            ok = self.client.post("/api/decisions", json={"request_id": "REQ-1001", "architecture": "rules", "reviewer": "Ann",
                                                          "action": "accept_routing", "justification": ""})
            self.assertEqual(ok.status_code, 200)

    def test_evaluation_endpoint(self):
        data = self.client.get("/api/evaluation").json()
        self.assertTrue(data["available"])
        self.assertEqual(len(data["cases"]), 33)
        self.assertIn("decision rule", data["decision_line"].lower())


if __name__ == "__main__":
    unittest.main()
