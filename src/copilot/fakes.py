"""Vendor-risk fetchers for tests and evaluation scenarios.

`in_process_fetcher` runs the real mock API app in-process (no network), so
tests exercise the same endpoint logic. The fault fetchers simulate failure
modes the public data only partially covers (timeouts, malformed payloads).
"""
from __future__ import annotations

import time
from urllib.parse import quote

from src.vendor_client import REQUIRED_FIELDS, VendorRiskFetcher, VendorRiskResult


def in_process_fetcher(overrides: dict[str, dict] | None = None) -> VendorRiskFetcher:
    from fastapi.testclient import TestClient

    from mock_api import app as mock_module

    client = TestClient(mock_module.app)
    overrides = overrides or {}

    def fetch(vendor_name: str) -> VendorRiskResult:
        path = f"/vendor-risk/{quote(vendor_name, safe='')}"
        endpoint = f"GET {path}"
        start = time.perf_counter()
        if vendor_name in overrides:
            record = overrides[vendor_name]
            mode = record.get("_mode")
            if mode == "timeout":
                return VendorRiskResult(False, "timeout", endpoint, error="Timed out after 3.0s", attempts=2)
            if mode == "malformed":
                return VendorRiskResult(False, "invalid_response", endpoint, data={"unexpected": True},
                                        error="Response lacks security_review_status", http_status=200, attempts=1)
            if mode == "not_found":
                return VendorRiskResult(False, "not_found", endpoint, error="No vendor-risk record exists for this vendor", http_status=404, attempts=1)
            if mode == "error":
                return VendorRiskResult(False, "unavailable", endpoint, error=record.get("error", "Service unavailable"), http_status=503, attempts=2)
            data = {"vendor_name": vendor_name, **{k: v for k, v in record.items() if not k.startswith("_")}}
            missing = [f for f in REQUIRED_FIELDS if f not in data]
            return VendorRiskResult(True, "ok", endpoint, data=data, http_status=200, attempts=1,
                                    warnings=[f"Response missing field '{m}'" for m in missing])
        response = client.get(path)
        latency = round((time.perf_counter() - start) * 1000, 1)
        if response.status_code == 404:
            return VendorRiskResult(False, "not_found", endpoint, error="No vendor-risk record exists for this vendor", http_status=404, attempts=1, latency_ms=latency)
        if response.status_code >= 500:
            detail = response.json().get("detail", "Vendor-risk service unavailable")
            return VendorRiskResult(False, "unavailable", endpoint, error=detail, http_status=response.status_code, attempts=2, latency_ms=latency)
        return VendorRiskResult(True, "ok", endpoint, data=response.json(), http_status=200, attempts=1, latency_ms=latency)

    return fetch
