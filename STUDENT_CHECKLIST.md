# Student submission checklist

Before submitting, confirm that:

- [x] `python verify_setup.py` passes in your project environment.
- [x] The product can process a request end-to-end (`python run_local.py` → UI, or `handle_request(...)`).
- [x] Architecture A is a working single-agent baseline (`src/copilot/pipeline.py::_run_single`).
- [x] Architecture B is a lightweight staged / 2-agent variant (`_run_staged`: Analyst → engine → Reviewer).
- [x] At least 3 tools are used; at least 1 tool/check is deterministic (8 tools; budget, catalog search, policy engine are deterministic).
- [x] Recommendations are returned in the `ProcurementDecision` structure.
- [x] Important evidence is visible to the user (evidence panel with source + reference per item).
- [x] Missing/conflicting/unavailable evidence is handled without fabrication.
- [x] Human approval is preserved for sensitive decisions (`human_review_required` always true; read-only tools; audited human actions).
- [x] Prompt injection inside business data does not override system behavior (scan + delimiting + escalate-only guardrails; tested).
- [x] Date-based checks use the policy's data snapshot / reference date (parsed from `procurement_policy.md`).
- [x] The same evaluation cases were run on both architectures (`evals/run_eval_suite.py`, 33 cases).
- [ ] **Latency and LLM/tool-call counts are reported with a live model**: run `python evals/run_eval_suite.py --repeats 3 --update-docs` with `ANTHROPIC_API_KEY` set.
- [x] The decision memo is <= 500 words and supported by evaluation evidence (the table fills automatically on the run above).
- [x] Setup instructions work from a clean environment.
- [x] Any LLM/provider SDK you added is present in `requirements.txt` (`anthropic`, `openai`).
- [x] `.env`, API keys, and other secrets are not committed (`.env` git-ignored; `.env.example` provided).
