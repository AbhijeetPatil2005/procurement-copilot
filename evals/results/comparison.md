_Generated 2026-10-06 22:39 · 33 cases × 1 repeat(s) · provider `openai` · model `gemini-3.1-flash-lite`_

| Metric | A · Single agent | B · Staged 2-agent | Rules-only baseline |
|---|---:|---:|---:|
| Cases passing all quality checks | 93.9% | 78.8% | 90.9% |
| Public minimum checks | 6/6 | 5/6 | 6/6 |
| Correct next action (status) | 97.0% | 78.8% | 97.0% |
| Required approvals correct | 97.0% | 100.0% | 97.0% |
| Approvals exact (no over-escalation) | 97.0% | 100.0% | 97.0% |
| Risk flags correct | 93.9% | 100.0% | 90.9% |
| Missing-info handling correct | 100.0% | 90.9% | 100.0% |
| Human review correct | 100.0% | 100.0% | 100.0% |
| Raw LLM policy compliance (pre-guardrail) | 100.0% | 100.0% | n/a |
| Guardrail corrections (total) | 1 | 11 | 0 |
| Agent evidence grounding rate | 98.6% | 86.8% | n/a |
| Ungrounded claims dropped | 2 | 32 | 0 |
| Avg latency (ms) | 37604.88 | 29110.91 | 16.47 |
| p95 latency (ms) | 71581.4 | 42783.7 | 49.3 |
| Avg LLM calls | 5.42 | 4.12 | 0.0 |
| Avg tool calls | 8.88 | 8.97 | 7.0 |
| Avg input / output tokens | 17264 / 705 | 9930 / 1404 | 0 / 0 |
| Avg cost per request (USD) | 0.0 | 0.0 | 0.0 |
| Consistency across repeats | n/a | n/a | n/a |
| Execution mode | llm: 33 | llm: 33 | rules_only: 33 |

- **A · Single agent** failed: EXT-31, EXT-32
- **B · Staged 2-agent** failed: EXT-01, EXT-03, EXT-10, EXT-11, EXT-12, EXT-13, EXT-15
- **Rules-only baseline** failed: EXT-31, EXT-32, EXT-33

**Pre-registered decision rule → ship A · Single agent:** B's pass rate differs by -15.1 pts (threshold +5); B was 23% faster with -1.3 LLM calls per request, which does not offset the quality gap.
