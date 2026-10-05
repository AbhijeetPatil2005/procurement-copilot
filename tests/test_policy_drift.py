"""Fails if procurement_policy.md changes in a way the encoded rules no longer match."""
from __future__ import annotations

import re
import unittest
from datetime import date

from src.config import POLICY_PATH, policy_reference_date, review_validity_days
from src.copilot import policy_engine as pe


class PolicyDriftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = POLICY_PATH.read_text(encoding="utf-8")

    def test_reference_date_and_validity_parsed_from_policy(self):
        self.assertEqual(policy_reference_date(), date(2026, 9, 30))
        self.assertEqual(review_validity_days(), 365)

    def test_threshold_table_matches_engine(self):
        rows = re.findall(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$", self.text, re.MULTILINE)
        table = {k: v for k, v in rows if "$" in k}
        expected = {label: approvals for _, approvals, label in pe.THRESHOLDS}
        self.assertEqual(len(table), len(expected), f"policy table rows: {table}")
        for label, approvals in expected.items():
            self.assertIn(label, table, f"tier '{label}' not found in policy")
            self.assertEqual(sorted(a.strip() for a in table[label].split("+")), sorted(approvals))

    def test_legal_threshold_matches(self):
        self.assertIn("annual spend is **$10,000 or more**", self.text)
        self.assertEqual(pe.LEGAL_NEW_VENDOR_THRESHOLD, 10_000)

    def test_security_triggers_present(self):
        for phrase in ["source code access", "production or cloud-account integration", "confidential documents",
                       "employee PII", "customer PII", "credentials/secrets", "missing, expired, or not completed"]:
            self.assertIn(phrase, self.text)


if __name__ == "__main__":
    unittest.main()
