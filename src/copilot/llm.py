"""Provider-neutral chat sessions with tool calling.

* `AnthropicSession` - Claude via the official `anthropic` SDK (default).
* `OpenAISession`    - OpenAI or any OpenAI-compatible endpoint via the `openai` SDK.
* `ScriptedSession`  - deterministic fake used by unit tests (no network).

A session owns its provider-specific message history; the agent loop only sees
`TurnResult`s. Any provider failure is raised as `LLMUnavailable` so the
pipeline can degrade to the deterministic path instead of crashing.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from src.config import Settings


class LLMUnavailable(RuntimeError):
    """Provider error, refusal, timeout, or missing credentials."""


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict


@dataclass
class TurnResult:
    text: str
    tool_uses: list[ToolUse]
    stop_reason: str
    usage: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    wait_ms: float = 0.0          # client-side throttling / rate-limit backoff (excluded from latency)


_PACE_LOCK = threading.Lock()
_LAST_REQUEST = [0.0]


def _pace(min_interval_s: float) -> float:
    """Space requests at least `min_interval_s` apart across threads (free-tier RPM limits). Returns ms waited."""
    if min_interval_s <= 0:
        return 0.0
    with _PACE_LOCK:
        wait = _LAST_REQUEST[0] + min_interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST[0] = time.monotonic()
    return max(0.0, wait) * 1000


class ChatSession(Protocol):
    provider: str
    model: str

    def send(self) -> TurnResult: ...
    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None: ...
    def add_user_text(self, text: str) -> None: ...


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
class AnthropicSession:
    provider = "anthropic"

    def __init__(self, settings: Settings, system: str, user_text: str, tools: list[dict], client: Any = None) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on env
                raise LLMUnavailable("anthropic SDK not installed (pip install -r requirements.txt)") from exc
            client = anthropic.Anthropic(timeout=settings.llm_timeout_s, max_retries=2)
        self.client = client
        self.model = settings.model_name
        self.effort = settings.llm_effort
        self.use_fallbacks = settings.anthropic_fallbacks and self.model.startswith(("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5"))
        self.use_strict = True
        self.system = [{"type": "text", "text": system}]
        self.tools = tools
        self.messages: list[dict] = [{"role": "user", "content": user_text}]

    def _tool_defs(self) -> list[dict]:
        defs = []
        for t in self.tools:
            d = {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            if self.use_strict:
                d["strict"] = True
            defs.append(d)
        return defs

    def _create(self):
        params: dict[str, Any] = dict(
            model=self.model,
            max_tokens=16000,
            system=self.system,
            tools=self._tool_defs(),
            messages=self.messages,
            cache_control={"type": "ephemeral"},   # cache stable system+tools prefix across turns
        )
        if self.model.startswith(("claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-4-6", "claude-sonnet-5", "claude-fable")):
            params["output_config"] = {"effort": self.effort}
        if self.use_fallbacks:
            return self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        return self.client.messages.create(**params)

    def send(self) -> TurnResult:
        import anthropic

        start = time.perf_counter()
        try:
            try:
                response = self._create()
            except anthropic.BadRequestError:
                # Degrade optional features (strict schemas / server fallbacks) once before giving up.
                if not (self.use_strict or self.use_fallbacks):
                    raise
                self.use_strict, self.use_fallbacks = False, False
                response = self._create()
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailable("Anthropic authentication failed - check ANTHROPIC_API_KEY") from exc
        except anthropic.NotFoundError as exc:
            raise LLMUnavailable(f"Model '{self.model}' not found") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable("Anthropic rate limit exceeded after retries") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailable(f"Anthropic API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable("Could not reach the Anthropic API") from exc
        latency = round((time.perf_counter() - start) * 1000, 1)

        if response.stop_reason == "refusal":
            raise LLMUnavailable("Model declined the request (refusal)")
        # Keep the full assistant content (incl. thinking blocks) for the next turn; drop fallback markers.
        content = [b for b in response.content if getattr(b, "type", "") != "fallback"]
        self.messages.append({"role": "assistant", "content": content})
        text = "".join(getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text")
        uses = [ToolUse(b.id, b.name, dict(b.input or {})) for b in content if getattr(b, "type", "") == "tool_use"]
        u = response.usage
        usage = {
            "input_tokens": getattr(u, "input_tokens", 0) or 0,
            "output_tokens": getattr(u, "output_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        return TurnResult(text, uses, response.stop_reason or "", usage, latency)

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        # All results of one assistant turn go back in a single user message.
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": content, **({"is_error": True} if err else {})}
            for tid, content, err in results
        ]})

    def add_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


# ---------------------------------------------------------------------------
# OpenAI / OpenAI-compatible
# ---------------------------------------------------------------------------
class OpenAISession:
    provider = "openai"

    def __init__(self, settings: Settings, system: str, user_text: str, tools: list[dict], client: Any = None) -> None:
        if client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover
                raise LLMUnavailable("openai SDK not installed (pip install openai)") from exc
            # Retries on 429/5xx are handled in send() so throttling waits can be measured and excluded from latency.
            kwargs: dict[str, Any] = {"timeout": settings.llm_timeout_s, "max_retries": 0}
            if settings.openai_base_url:
                kwargs["base_url"] = settings.openai_base_url
            if settings.openai_auth_header:  # some gateways (e.g. YepAPI) authenticate with x-api-key, not Bearer
                kwargs["default_headers"] = {settings.openai_auth_header: os.getenv("OPENAI_API_KEY", "")}
            client = openai.OpenAI(**kwargs)
        self.client = client
        self.model = settings.model_name
        self.min_interval_s = settings.llm_min_interval_s
        self.retries = settings.llm_rate_limit_retries
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                         "parameters": t["input_schema"]}} for t in tools]
        self.messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user_text}]

    def _create(self) -> tuple[Any, float]:
        """Call the endpoint with pacing and backoff on 429/5xx. Returns (response, ms spent waiting)."""
        import openai

        waited = _pace(self.min_interval_s)
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.chat.completions.create(model=self.model, messages=self.messages, tools=self.tools,
                                                           tool_choice="auto", temperature=0)
                return resp, waited
            except (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError) as exc:
                # A daily quota will not reset within any reasonable backoff - fail fast instead of waiting.
                if attempt >= self.retries or re.search(r"PerDay|per day|daily", str(exc), re.IGNORECASE):
                    raise
                hint = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", str(exc))
                delay = min(90.0, float(hint.group(1)) + 1 if hint else 5.0 * 2 ** attempt)
                time.sleep(delay)
                waited += delay * 1000 + _pace(self.min_interval_s)
        raise AssertionError("unreachable")

    def send(self) -> TurnResult:
        import openai

        start = time.perf_counter()
        try:
            resp, waited_ms = self._create()
        except openai.AuthenticationError as exc:
            raise LLMUnavailable("OpenAI authentication failed - check OPENAI_API_KEY") from exc
        except openai.APIStatusError as exc:
            raise LLMUnavailable(f"OpenAI API error {exc.status_code}: {str(exc.message)[:200]}") from exc
        except openai.APIConnectionError as exc:
            raise LLMUnavailable("Could not reach the OpenAI-compatible endpoint") from exc
        latency = round((time.perf_counter() - start) * 1000 - waited_ms, 1)
        choice = resp.choices[0]
        msg = choice.message
        # Echo the assistant message back unchanged: some providers attach extra fields to tool
        # calls (e.g. Gemini 3 thought signatures) that must be returned on the next turn.
        assistant: dict[str, Any] = msg.model_dump(exclude_none=True)
        assistant["role"] = "assistant"
        assistant.setdefault("content", "")
        uses: list[ToolUse] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {"__invalid_json__": tc.function.arguments}
                uses.append(ToolUse(tc.id, tc.function.name, args if isinstance(args, dict) else {}))
        self.messages.append(assistant)
        u = resp.usage
        usage = {"input_tokens": getattr(u, "prompt_tokens", 0) or 0, "output_tokens": getattr(u, "completion_tokens", 0) or 0}
        return TurnResult(msg.content or "", uses, choice.finish_reason or "", usage, latency, wait_ms=round(waited_ms, 1))

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        for tid, content, _err in results:
            self.messages.append({"role": "tool", "tool_call_id": tid, "content": content})

    def add_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


# ---------------------------------------------------------------------------
# Scripted (tests)
# ---------------------------------------------------------------------------
ScriptStep = Callable[["ScriptedSession"], TurnResult]


class ScriptedSession:
    """Replays scripted turns. Each step receives the session (to inspect tool results)."""

    provider = "scripted"
    model = "scripted"

    def __init__(self, steps: list[ScriptStep]) -> None:
        self.steps = list(steps)
        self.tool_results: list[list[tuple[str, str, bool]]] = []
        self.user_texts: list[str] = []

    def send(self) -> TurnResult:
        if not self.steps:
            return TurnResult("", [], "end_turn")
        return self.steps.pop(0)(self)

    def add_tool_results(self, results: list[tuple[str, str, bool]]) -> None:
        self.tool_results.append(results)

    def add_user_text(self, text: str) -> None:
        self.user_texts.append(text)


SessionFactory = Callable[[str, str, list[dict]], ChatSession]


def default_session_factory(settings: Settings) -> SessionFactory | None:
    if settings.llm_provider == "anthropic":
        return lambda system, user, tools: AnthropicSession(settings, system, user, tools)
    if settings.llm_provider == "openai":
        return lambda system, user, tools: OpenAISession(settings, system, user, tools)
    return None


# Approximate list prices (USD per 1M tokens) for cost reporting only.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-5": (5.0, 25.0), "claude-fable-5-1": (10.0, 50.0), "gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.5, 10.0),
}


def estimate_cost_usd(model: str, usage: dict) -> float | None:
    price = PRICES.get(model)
    if not price:
        return None
    inp = usage.get("input_tokens", 0) + 1.25 * usage.get("cache_creation_input_tokens", 0) + 0.1 * usage.get("cache_read_input_tokens", 0)
    return round((inp * price[0] + usage.get("output_tokens", 0) * price[1]) / 1_000_000, 5)
