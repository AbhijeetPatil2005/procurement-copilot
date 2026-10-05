"""The agent loop shared by every LLM role.

A role = (system prompt, allowed tools, submit tool, output model). The loop:
send -> execute requested tools (all results of one turn returned together) ->
repeat until the submit tool is called with a valid payload, or the turn budget
is exhausted. Invalid submissions get a validation error back and one more try.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from src.copilot.llm import ChatSession, LLMUnavailable
from src.copilot.prompts import untrusted_block
from src.copilot.tools import TOOLS, ToolContext

T = TypeVar("T", bound=BaseModel)

# Tool outputs that carry free text from business data are wrapped as untrusted.
_UNTRUSTED_TOOLS = {"get_purchase_request", "get_vendor_registry_record", "get_vendor_risk_assessment", "search_existing_software"}
MAX_RESULT_CHARS = 12_000


class AgentIncomplete(RuntimeError):
    """The agent did not submit a valid result within its turn budget."""


@dataclass
class AgentRun:
    role: str
    llm_calls: int = 0
    tool_calls: int = 0
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0,
                                                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
    llm_latency_ms: float = 0.0
    transcript: list[dict] = field(default_factory=list)
    validation_errors: int = 0

    def add_usage(self, usage: dict) -> None:
        for k, v in usage.items():
            self.usage[k] = self.usage.get(k, 0) + (v or 0)


def _render(name: str, output: dict) -> str:
    text = json.dumps(output, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + '..."[truncated]"'
    return untrusted_block(name, text) if name in _UNTRUSTED_TOOLS else text


def run_agent(
    session: ChatSession,
    ctx: ToolContext,
    role: str,
    allowed_tools: list[str],
    submit_tool: str,
    output_model: type[T],
    max_turns: int,
) -> tuple[T, AgentRun]:
    run = AgentRun(role=role)
    try:
        nudged = False
        for _turn in range(max_turns):
            turn = session.send()
            run.llm_calls += 1
            run.add_usage(turn.usage)
            run.llm_latency_ms += turn.latency_ms
            run.transcript.append({"type": "llm", "text": turn.text[:2000], "stop_reason": turn.stop_reason,
                                   "tool_uses": [{"name": u.name, "input": u.input} for u in turn.tool_uses],
                                   "usage": turn.usage, "latency_ms": turn.latency_ms})

            if not turn.tool_uses:
                if turn.stop_reason == "max_tokens":
                    raise AgentIncomplete(f"{role}: response truncated at max_tokens")
                if nudged:
                    raise AgentIncomplete(f"{role}: ended without calling {submit_tool}")
                session.add_user_text(f"Call `{submit_tool}` now with your structured result.")
                nudged = True
                continue

            results: list[tuple[str, str, bool]] = []
            submitted: T | None = None
            for use in turn.tool_uses:
                if use.name == submit_tool:
                    try:
                        submitted = output_model.model_validate(use.input)
                        results.append((use.id, "Submission received.", False))
                    except ValidationError as exc:
                        run.validation_errors += 1
                        msg = f"Submission rejected - fix these schema errors and call {submit_tool} again: {exc.errors()[:6]}"
                        results.append((use.id, msg, True))
                        run.transcript.append({"type": "validation_error", "detail": str(exc)[:1500]})
                    continue
                if use.name not in allowed_tools or use.name not in TOOLS:
                    results.append((use.id, f"Tool '{use.name}' is not available to the {role}. Allowed: {allowed_tools}", True))
                    continue
                out = ctx.call(use.name, use.input, caller=role)
                run.tool_calls += 1
                run.transcript.append({"type": "tool", "name": use.name, "input": use.input, "ok": bool(out.get("ok"))})
                results.append((use.id, _render(use.name, out), not out.get("ok", False)))

            if submitted is not None:
                return submitted, run
            session.add_tool_results(results)
        raise AgentIncomplete(f"{role}: no valid {submit_tool} within {max_turns} turns")
    except (LLMUnavailable, AgentIncomplete) as exc:
        exc.run = run  # type: ignore[attr-defined]  - keep telemetry of the failed attempt
        raise


__all__ = ["run_agent", "AgentRun", "AgentIncomplete", "LLMUnavailable"]
