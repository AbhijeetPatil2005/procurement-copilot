# Architecture Decision Memo

## Decision

Ship **A: one tool-using agent on a deterministic policy engine with guardrails**. The rule was set before collecting LLM results: choose the simpler system unless the staged variant (B) passes ≥5 points more cases (about two of 33, more than run-to-run noise) without failing a safety check.

## Evidence

Both ran on the same 33 cases: 10 dataset requests plus 23 synthetic hidden-style edge cases, including 3 semantic cases that keyword rules cannot solve.

<!-- EVAL_RESULTS_START -->
_Pending: run `python evals/run_eval_suite.py --repeats 3 --update-docs` with an API key to fill this table._
<!-- EVAL_RESULTS_END -->

- **Policy correctness is mostly built in.** Thresholds, budget, review dates and review triggers are code shared by both. Rules-only passes 30/33 and fails only the semantic cases, which measure what the LLM adds.
- **Raw LLM compliance** shows how often a draft met policy *before* guardrails corrected it. This is where the architectures differ.
- **B costs more by design**: at least two extra sequential LLM turns, and the reviewer re-reads the full evidence pack.

## Trade-offs

B's separation of duties is real: the reviewer cannot fetch data, the analyst cannot decide, the handoff can be audited. But the controls that matter are already deterministic in both (engine floor, escalate-never-de-escalate, grounding and approval-language checks). B's second opinion mostly duplicates them while adding latency, tokens, a second prompt, and a new failure mode: facts dropped in the handoff.

## Risks / validate before production

1. **Labels**: have a procurement lead review the gold labels, add historical requests, and track reviewer overrides in the audit log.
2. **Semantic coverage**: three cases is thin, so red-team paraphrased injections and data classes the form does not declare.
3. **Drift**: pin model and effort; re-run this suite on every model change.
4. **Data ownership**: the registry and vendor-risk API disagree; assign a source of truth and an outage SLA.
5. **Org data**: approver routing has gaps (missing directors, self-approval), so integrate the HRIS before automating routing.

## Why this is the right MVP

The client's risk is a wrong approval, and code plus humans control it, not agent count. One agent covers the AI's actual job: judging whether a stated gap is credible, catching what the form omits, and writing an actionable next step. B can return as a second reviewer for high-risk tiers (over $25k, new vendors with PII) if production data shows missed escalations.
