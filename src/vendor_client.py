from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote

import requests

# Fields the copilot relies on. A 200 response missing these is treated as
# "unverifiable", never as a favourable status.
REQUIRED_FIELDS = ("security_review_status", "last_review_date", "risk_level")


def get_vendor_risk(vendor_name: str, timeout_seconds: float = 3.0) -> dict:
    """Original low-level client (kept for compatibility). Raises on failure."""
    base_url = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    url = f"{base_url}/vendor-risk/{quote(vendor_name, safe='')}"
    response = requests.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()


@dataclass
class VendorRiskResult:
    """Outcome of a vendor-risk lookup. Never raises; failures are explicit."""

    ok: bool
    status: str                      # ok | not_found | unavailable | timeout | connection_error | invalid_response
    endpoint: str
    data: dict[str, Any] | None = None
    error: str | None = None
    http_status: int | None = None
    attempts: int = 0
    latency_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "endpoint": self.endpoint,
            "http_status": self.http_status,
            "data": self.data,
            "error": self.error,
            "attempts": self.attempts,
            "warnings": self.warnings,
        }


class VendorRiskClient:
    """Resilient client for the vendor-risk service.

    * bounded timeout, retry only on transient failures (5xx / network)
    * 404 is a definitive "no record" (not retried)
    * response schema is validated; missing fields become warnings
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout_s: float = 3.0,
        retries: int = 1,
        backoff_s: float = 0.25,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001")).rstrip("/")
        self.timeout_s = timeout_s
        self.retries = max(0, retries)
        self.backoff_s = backoff_s
        self.session = session or requests.Session()

    def fetch(self, vendor_name: str) -> VendorRiskResult:
        path = f"/vendor-risk/{quote(vendor_name, safe='')}"
        endpoint = f"GET {path}"
        url = f"{self.base_url}{path}"
        start = time.perf_counter()
        last: VendorRiskResult | None = None
        for attempt in range(1, self.retries + 2):
            last = self._attempt(url, endpoint, attempt)
            if last.status not in {"unavailable", "timeout", "connection_error"}:
                break
            if attempt <= self.retries:
                time.sleep(self.backoff_s * attempt)
        assert last is not None
        last.latency_ms = round((time.perf_counter() - start) * 1000, 1)
        return last

    def _attempt(self, url: str, endpoint: str, attempt: int) -> VendorRiskResult:
        try:
            response = self.session.get(url, timeout=self.timeout_s)
        except requests.Timeout:
            return VendorRiskResult(False, "timeout", endpoint, error=f"Timed out after {self.timeout_s}s", attempts=attempt)
        except requests.RequestException as exc:
            return VendorRiskResult(False, "connection_error", endpoint, error=f"{type(exc).__name__}: service unreachable", attempts=attempt)

        if response.status_code == 404:
            return VendorRiskResult(False, "not_found", endpoint, error="No vendor-risk record exists for this vendor", http_status=404, attempts=attempt)
        if response.status_code >= 500:
            detail = _detail(response) or "Vendor-risk service unavailable"
            return VendorRiskResult(False, "unavailable", endpoint, error=detail, http_status=response.status_code, attempts=attempt)
        if not response.ok:
            return VendorRiskResult(False, "invalid_response", endpoint, error=_detail(response) or response.reason, http_status=response.status_code, attempts=attempt)
        try:
            payload = response.json()
        except ValueError:
            return VendorRiskResult(False, "invalid_response", endpoint, error="Response body is not valid JSON", http_status=response.status_code, attempts=attempt)
        if not isinstance(payload, dict):
            return VendorRiskResult(False, "invalid_response", endpoint, error="Unexpected response shape", http_status=response.status_code, attempts=attempt)

        missing = [f for f in REQUIRED_FIELDS if f not in payload]
        if "security_review_status" in missing:
            return VendorRiskResult(False, "invalid_response", endpoint, data=payload, error="Response lacks security_review_status", http_status=response.status_code, attempts=attempt)
        warnings = [f"Response missing field '{f}'" for f in missing]
        return VendorRiskResult(True, "ok", endpoint, data=payload, http_status=response.status_code, attempts=attempt, warnings=warnings)


def _detail(response: requests.Response) -> str | None:
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("detail") or "") or None
    except ValueError:
        return None
    return None


# A fetcher is anything that maps a vendor name to a VendorRiskResult. The
# evaluation harness injects fakes (timeouts, malformed payloads) through this.
VendorRiskFetcher = Callable[[str], VendorRiskResult]
