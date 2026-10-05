_Generated 2026-10-05 23:22 · 33 cases × 1 repeat(s) · provider `none` · model `n/a`_

| Metric | Rules-only baseline |
|---|---:|
| Cases passing all quality checks | 90.9% |
| Public minimum checks | 6/6 |
| Correct next action (status) | 97.0% |
| Required approvals correct | 97.0% |
| Approvals exact (no over-escalation) | 97.0% |
| Risk flags correct | 90.9% |
| Missing-info handling correct | 100.0% |
| Human review correct | 100.0% |
| Raw LLM policy compliance (pre-guardrail) | n/a |
| Guardrail corrections (total) | 0 |
| Agent evidence grounding rate | n/a |
| Ungrounded claims dropped | 0 |
| Avg latency (ms) | 11.53 |
| p95 latency (ms) | 34.8 |
| Avg LLM calls | 0.0 |
| Avg tool calls | 7.0 |
| Avg input / output tokens | 0 / 0 |
| Avg cost per request (USD) | 0.0 |
| Consistency across repeats | n/a |
| Execution mode | rules_only: 33 |

- **Rules-only baseline** failed: EXT-31, EXT-32, EXT-33

**Decision rule:** needs both `single` and `staged` results.
