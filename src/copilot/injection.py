"""Deterministic detection of instructions embedded in business data.

This is one layer of defence, not the only one:

1. Untrusted text is delimited and labelled as data in every prompt.
2. This scanner flags instruction-like text so reviewers see it (policy section 9).
3. Guardrails make injection *ineffective* even if undetected: the LLM can only
   add approvals / flags / caution, never remove what the policy engine requires,
   and it can never mark a request as approved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# (label, pattern). Patterns target attempts to change rules, bypass controls,
# fabricate approval, or exfiltrate secrets - not ordinary urgency language.
_PATTERNS: list[tuple[str, str]] = [
    ("override_instructions", r"\b(ignore|disregard|forget|override|bypass|skip)\b[^.\n]{0,40}\b(rules?|polic(y|ies)|instructions?|procedures?|controls?|guidelines?|checks?|reviews?|process)\b"),
    ("fabricated_approval", r"\b(treat|mark|consider|record|log|flag)\b[^.\n]{0,40}\b(as\s+)?(already\s+)?(cfo|ceo|vp|finance|security|legal|pre)[\s-]*(approved|signed[\s-]off|authori[sz]ed)\b"),
    ("fabricated_approval", r"\b(already|pre)[\s-]*(approved|authori[sz]ed|signed[\s-]off)\b"),
    ("fabricated_approval", r"\bas\s+(the\s+)?(cfo|ceo|vp|head of [a-z]+|procurement|security)\b[^.\n]{0,30}\b(i\s+)?(approve|authori[sz]e)"),
    ("forced_decision", r"\b(approve|auto[\s-]?approve|fast[\s-]?track)\b[^.\n]{0,30}\b(immediately|now|automatically|without (review|approval))\b"),
    ("suppress_controls", r"\b(do not|don't|never|no need to)\b[^.\n]{0,30}\b(flag|escalate|route|review|involve|notify|check)\b"),
    ("role_manipulation", r"\b(you are now|act as|pretend to be|from now on you|new instructions|system prompt|developer mode|jailbreak)\b"),
    ("secret_exfiltration", r"\b(reveal|print|show|expose|send|output)\b[^.\n]{0,30}\b(api[\s_-]?keys?|secrets?|passwords?|credentials?|tokens?|system prompt)\b"),
    ("markup_injection", r"(<\s*/?\s*(system|instructions?|assistant)\s*>|\[\s*(system|inst)\s*\]|^\s*(system|assistant)\s*:)"),
]
_COMPILED = [(label, re.compile(p, re.IGNORECASE | re.MULTILINE)) for label, p in _PATTERNS]


@dataclass(frozen=True)
class InjectionFinding:
    field: str
    label: str
    excerpt: str

    def to_dict(self) -> dict:
        return {"field": self.field, "label": self.label, "excerpt": self.excerpt}


def scan_text(field: str, text: str | None) -> list[InjectionFinding]:
    if not text or not isinstance(text, str):
        return []
    findings: list[InjectionFinding] = []
    seen: set[tuple[str, str]] = set()
    for label, pattern in _COMPILED:
        for match in pattern.finditer(text):
            excerpt = match.group(0).strip()
            key = (label, excerpt.lower())
            if key in seen:
                continue
            seen.add(key)
            findings.append(InjectionFinding(field=field, label=label, excerpt=excerpt[:120]))
    return findings


def scan_record(prefix: str, record: dict | None, fields: list[str] | None = None) -> list[InjectionFinding]:
    """Scan every string field (or the given ones) of a record."""
    if not record:
        return []
    findings: list[InjectionFinding] = []
    for key, value in record.items():
        if fields is not None and key not in fields:
            continue
        if isinstance(value, str):
            findings.extend(scan_text(f"{prefix}.{key}", value))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, str):
                    findings.extend(scan_text(f"{prefix}.{key}[{i}]", item))
    return findings


def strip_injected_sentences(text: str | None) -> str:
    """Return the text without sentences that contain injection patterns.

    Used only to judge whether a *genuine* business purpose remains (policy
    section 1) - the original text is always preserved for reviewers.
    """
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [s for s in sentences if not scan_text("_", s)]
    return " ".join(kept).strip()
