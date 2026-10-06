# Architecture Decision Memo

## Decision

Ship **A: one tool-using agent on a deterministic policy engine with guardrails**. The rule was set before any LLM results: keep the simpler system unless B passes ≥5 points more cases without failing a safety check. B scored 15 points lower.

## Evidence

Same 33 cases for both (10 dataset requests, 23 synthetic edge cases, 3 of them semantic), on Gemini 3.1 Flash-Lite (free tier, so $0).

<!-- EVAL_RESULTS_START -->
_33 cases x 1 repeat(s), openai gemini-3.1-flash-lite_

| Metric | Single (A) | Staged (B) | Rules only |
|---|---:|---:|---:|
| Cases passing quality criteria | 93.9% | 78.8% | 90.9% |
| Evidence grounding rate | 98.6% | 86.8% | n/a |
| Avg latency (s) | 37.6 | 29.1 | 0.0 |
| Avg LLM calls | 5.42 | 4.12 | 0.0 |

**Pre-registered decision rule → ship A · Single agent:** B's pass rate differs by -15.1 pts (threshold +5); B was 23% faster with -1.3 LLM calls per request, which does not offset the quality gap.
<!-- EVAL_RESULTS_END -->

- **B failed 7 routine cases, including public PUB-01.** The analyst's speculative open questions ("could unused seats be reallocated?") became blocking clarifications on complete, low-risk requests: the handoff amplified uncertainty.
- **B's evidence was less grounded.** 32 claims cited a policy tool the reviewer never called: wrong attribution, not invented numbers.
- **B caught all 3 semantic cases; A missed 2**: a paraphrased injection (EXT-31) and customer PII mentioned only in the text (EXT-32).
- **Cost was not the differentiator.** A took more turns, so B was faster. The shared engine set approvals correctly for both.

## Trade-offs

The two architectures fail differently:
- **A under-escalates rarely.** On EXT-32 it did not route Security and Privacy. That is the riskier error, though no request was approved and a human still reviews every case.
- **B over-escalates often.** It stalled 21% of cases with unnecessary questions.

B also adds a second prompt and a handoff to maintain. Closing A's gap with a targeted check beats accepting B's friction everywhere.

## Risks / validate before production

1. **Sample size**: 1 repeat on a lite model. Re-run with ≥3 repeats and a stronger model before treating the differences as settled.
2. **A's semantic misses**: add a detector for personal data mentioned in the justification and for paraphrased injection, then red-team more phrasings.
3. **B is cheap to fix**: only policy-required information should block, and open questions should become notes. Re-evaluate B if semantic recall matters more than throughput.
4. **Labels and data**: procurement reviews the gold labels; name a vendor-data source of truth; pin the model.

## Why this is the right MVP

Code and humans control wrong approvals in both. A chose the correct next action on 97% of cases with fewer moving parts; B's extra stage mostly added friction. Keep B's reviewer as an optional second opinion for high-risk requests (over $25k, or new vendors handling PII).
