"""Procurement Copilot web app: JSON API + single-page reviewer UI.

Presentation layer only. Every decision comes from src/copilot (tools, policy
engine, agents, guardrails); this module exposes it over HTTP and serves the
static frontend in web/static.

    uvicorn web.server:app --port 8501      (python run_local.py does this for you)
"""
from __future__ import annotations

import csv
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.audit import HUMAN_ACTIONS, read_log, record_human_action
from src.config import get_settings, policy_reference_date
from src.copilot.pipeline import CaseResult, analyze
from src.copilot.policy_engine import STATUS_LABELS
from src.copilot.report import approval_packet
from src.data_access import DataRepository, default_repository
from src.mock_service import is_healthy

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
RESULTS = ROOT / "evals" / "results"

ArchitectureName = Literal["single", "staged", "rules"]
ARCH_LABELS = {"single": "A · Single agent", "staged": "B · Staged (Analyst → Reviewer)", "rules": "Rules only (baseline)"}

app = FastAPI(title="Procurement Request Copilot", version="1.0")


class _State:
    """In-memory session state for a single local reviewer."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.repo: DataRepository = default_repository()
        self.results: dict[tuple[str, str], tuple[dict, dict]] = {}   # (request_id, arch) -> (payload, CaseResult-derived extras)
        self.latest_status: dict[str, dict] = {}

    def clone_repo(self) -> DataRepository:
        with self.lock:
            return self.repo.clone()


STATE = _State()


def _serialize(result: CaseResult) -> dict:
    status = result.guardrails.final_status or ""
    trace = result.trace()
    return {
        "request_id": result.decision.request_id,
        "architecture": result.architecture,
        "architecture_label": ARCH_LABELS.get(result.architecture, result.architecture),
        "status": status,
        "status_label": STATUS_LABELS.get(status, "Recommendation"),
        "decision": result.decision.model_dump(),
        **trace,
    }


def _run(request_id: str, architecture: str) -> tuple[dict, CaseResult]:
    result = analyze(request_id, architecture=architecture, repo=STATE.clone_repo())  # type: ignore[arg-type]
    payload = _serialize(result)
    with STATE.lock:
        STATE.results[(request_id, architecture)] = (payload, {"result": result})
        STATE.latest_status[request_id] = {"status": payload["status"], "status_label": payload["status_label"],
                                           "architecture": architecture}
    return payload, result


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/meta")
def meta() -> dict:
    s = get_settings()
    return {
        "vendor_api_online": is_healthy(s.vendor_risk_base_url),
        "llm_enabled": s.llm_enabled,
        "provider": s.llm_provider,
        "model": s.model_name if s.llm_enabled else None,
        "policy_reference_date": policy_reference_date().isoformat(),
        "architectures": ARCH_LABELS,
        "human_actions": HUMAN_ACTIONS,
    }


@app.get("/api/requests")
def list_requests() -> list[dict]:
    with STATE.lock:
        out = []
        for r in STATE.repo.requests:
            emp = STATE.repo.get_employee(r.get("requester_id")) or {}
            out.append({**r, "requester_name": emp.get("name"), "department": emp.get("department"),
                        "last_result": STATE.latest_status.get(r["request_id"])})
        return out


class NewRequest(BaseModel):
    requester_id: str
    product_name: str = Field(default="", max_length=120)
    vendor_name: str = Field(default="", max_length=120)
    category: str = Field(default="", max_length=80)
    annual_cost_usd: float | None = Field(default=None, ge=0)
    user_count: int | None = Field(default=None, ge=0)
    business_justification: str = Field(default="", max_length=2000)
    data_access_level: str = Field(default="unknown", max_length=60)
    requested_integrations: list[str] = Field(default_factory=list, max_length=20)
    urgency: Literal["normal", "high", "urgent"] = "normal"


@app.post("/api/requests")
def create_request(body: NewRequest) -> dict:
    with STATE.lock:
        if STATE.repo.get_employee(body.requester_id) is None:
            raise HTTPException(400, f"Unknown requester '{body.requester_id}'")
        n = len([r for r in STATE.repo.requests if r["request_id"].startswith("NEW-")]) + 1
        request = {"request_id": f"NEW-{n:03d}", **body.model_dump()}
        request["user_count"] = request["user_count"] or None
        STATE.repo.upsert_request(request)
    return request


@app.get("/api/employees")
def employees() -> list[dict]:
    return [{"employee_id": e["employee_id"], "name": e["name"], "department": e["department"]} for e in STATE.repo.employees]


class AnalyzeBody(BaseModel):
    request_id: str
    architecture: ArchitectureName = "single"


@app.post("/api/analyze")
def analyze_request(body: AnalyzeBody) -> dict:
    if STATE.repo.get_request(body.request_id) is None:
        raise HTTPException(404, f"No request '{body.request_id}'")
    payload, _ = _run(body.request_id, body.architecture)
    return payload


@app.get("/api/results/{request_id}")
def cached_results(request_id: str) -> dict:
    with STATE.lock:
        return {arch: payload for (rid, arch), (payload, _) in STATE.results.items() if rid == request_id}


class CompareBody(BaseModel):
    request_id: str


@app.post("/api/compare")
def compare(body: CompareBody) -> dict:
    if STATE.repo.get_request(body.request_id) is None:
        raise HTTPException(404, f"No request '{body.request_id}'")
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {a: pool.submit(_run, body.request_id, a) for a in ARCH_LABELS}
        return {a: f.result()[0] for a, f in futures.items()}


@app.get("/api/packet/{request_id}/{architecture}", response_class=PlainTextResponse)
def packet(request_id: str, architecture: str) -> PlainTextResponse:
    with STATE.lock:
        entry = STATE.results.get((request_id, architecture))
        request = STATE.repo.get_request(request_id) or {}
    if not entry:
        raise HTTPException(404, "Analyze the request first")
    text = approval_packet(entry[1]["result"], request)
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="{request_id}_review_packet.md"'})


class DecisionBody(BaseModel):
    request_id: str
    architecture: ArchitectureName
    reviewer: str = Field(max_length=120)
    action: str
    justification: str = Field(default="", max_length=2000)


@app.post("/api/decisions")
def record_decision(body: DecisionBody) -> dict:
    with STATE.lock:
        entry = STATE.results.get((body.request_id, body.architecture))
    if not entry:
        raise HTTPException(400, "Analyze the request before recording a decision")
    d = entry[0]["decision"]
    try:
        return record_human_action(request_id=body.request_id, reviewer=body.reviewer, action=body.action,
                                   justification=body.justification, architecture=body.architecture,
                                   copilot_recommendation=d["recommendation"], required_approvals=d["required_approvals"],
                                   risk_flags=d["risk_flags"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/decisions")
def decisions() -> list[dict]:
    return list(reversed(read_log()))


@app.get("/api/evaluation")
def evaluation() -> dict:
    summary_path = RESULTS / "summary.json"
    if not summary_path.exists():
        return {"available": False}
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    comparison = (RESULTS / "comparison.md").read_text(encoding="utf-8") if (RESULTS / "comparison.md").exists() else ""
    decision_line = next((ln.replace("**", "").strip() for ln in comparison.splitlines() if "decision rule" in ln.lower()), "")
    cases: dict[str, dict] = {}
    for arch in ("single", "staged", "rules"):
        path = RESULTS / f"runs_{arch}.csv"
        if not path.exists():
            continue
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                c = cases.setdefault(row["case_id"], {"case_id": row["case_id"], "edge_case": row["edge_case"],
                                                      "title": row["title"], "results": {}})
                c["results"][arch] = {"passed": row.get("passed") == "True", "status": row.get("final_status", ""),
                                      "latency_ms": row.get("latency_ms", "")}
    return {"available": True, "meta": data["meta"], "summary": data["summary"], "decision_line": decision_line,
            "cases": sorted(cases.values(), key=lambda c: c["case_id"])}


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
