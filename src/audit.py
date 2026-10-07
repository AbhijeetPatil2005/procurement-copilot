"""Append-only log of human reviewer actions (the copilot itself never approves)."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.config import RUNTIME_DIR

AUDIT_PATH = RUNTIME_DIR / "audit_log.jsonl"
HUMAN_ACTIONS = {
    "accept_routing": "Accept recommendation - send to the listed approvers",
    "return_to_requester": "Return to requester for clarification",
    "override": "Override recommendation (justification required)",
    "reject": "Reject request (justification required)",
}
_LOCK = threading.Lock()


def record_human_action(*, request_id: str, reviewer: str, action: str, justification: str, architecture: str,
                        copilot_recommendation: str, required_approvals: list[str], risk_flags: list[str],
                        path: Path | None = None) -> dict:
    if action not in HUMAN_ACTIONS:
        raise ValueError(f"Unknown action '{action}'")
    if not reviewer.strip():
        raise ValueError("Reviewer name is required")
    if action in {"override", "reject"} and len(justification.strip()) < 10:
        raise ValueError("A justification of at least 10 characters is required to override or reject")
    entry = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request_id": request_id,
        "reviewer": reviewer.strip(),
        "action": action,
        "justification": justification.strip(),
        "architecture": architecture,
        "copilot_recommendation": copilot_recommendation,
        "required_approvals": required_approvals,
        "risk_flags": risk_flags,
        "note": "Recorded reviewer action. Purchasing and approvals happen in the procurement system, not in the copilot.",
    }
    path = path or AUDIT_PATH  # resolved at call time so tests can redirect it
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    return entry


def read_log(path: Path | None = None) -> list[dict]:
    path = path or AUDIT_PATH
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
