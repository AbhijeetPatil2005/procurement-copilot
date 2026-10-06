"""Extended, reproducible evaluation of both architectures on the same test set.

    python evals/run_eval_suite.py                       # single + staged + rules, 1 repeat
    python evals/run_eval_suite.py --repeats 3 --workers 4
    python evals/run_eval_suite.py --architectures rules # deterministic baseline only (no key needed)
    python evals/run_eval_suite.py --update-docs         # write the comparison table into README + memo

Scores every run against hand-labelled gold expectations (evals/cases/extended_cases.json),
plus the official public expectations for the six public cases. Outputs go to
evals/results/: per-run CSVs, summary.json, comparison.md, evaluation_results.csv
(template format) and full JSON traces for qualitative review.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.run_public_evals import evaluate as public_evaluate  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.contracts import ProcurementDecision  # noqa: E402
from src.copilot.fakes import in_process_fetcher  # noqa: E402
from src.copilot.guardrails import _UNSAFE_LANGUAGE  # noqa: E402
from src.copilot.pipeline import CaseResult, analyze  # noqa: E402
from src.data_access import default_repository  # noqa: E402

CASES_PATH = ROOT / "evals" / "cases" / "extended_cases.json"
PUBLIC_PATH = ROOT / "evals" / "public_cases.json"
OUT_DIR = ROOT / "evals" / "results"
MARK_START, MARK_END = "<!-- EVAL_RESULTS_START -->", "<!-- EVAL_RESULTS_END -->"


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
def build_inputs(case: dict):
    repo = default_repository()
    for vendor_name, fields in (case.get("vendor_registry_overrides") or {}).items():
        for v in repo.vendors:
            if v["vendor_name"] == vendor_name:
                v.update(fields)
    if case.get("request"):
        repo.upsert_request(case["request"])
    request_id = case.get("request_id") or case["request"]["request_id"]
    fetcher = in_process_fetcher(case.get("vendor_risk_overrides"))
    return request_id, repo, fetcher


def run_one(case: dict, architecture: str, repeat: int) -> dict:
    request_id, repo, fetcher = build_inputs(case)
    start = time.perf_counter()
    try:
        result = analyze(request_id, architecture=architecture, repo=repo, vendor_fetcher=fetcher)  # type: ignore[arg-type]
        error = None
    except Exception as exc:  # the harness must survive implementation bugs and report them
        result, error = None, f"{type(exc).__name__}: {exc}"
    wall = round((time.perf_counter() - start) * 1000, 1)
    return {"case": case, "architecture": architecture, "repeat": repeat, "result": result, "error": error, "wall_ms": wall}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _contains_group(items: list[str], group: list[str]) -> bool:
    texts = [str(i).lower() for i in items]
    return any(tok.lower() in t for t in texts for tok in group)


def score(run: dict, public_cases: dict[str, dict]) -> dict:
    case, res = run["case"], run["result"]
    gold = case["gold"]
    row = {"case_id": case["case_id"], "edge_case": case["edge_case"], "title": case["title"],
           "architecture": run["architecture"], "repeat": run["repeat"], "error": run["error"] or ""}
    if res is None:
        row.update(passed=False, schema_valid=False)
        return row
    res: CaseResult
    d = res.decision
    try:
        ProcurementDecision.model_validate(d.model_dump())
        schema_valid = True
    except Exception:
        schema_valid = False
    status = res.guardrails.final_status
    approvals, flags = set(d.required_approvals), set(d.risk_flags)
    req_a, forb_a = set(gold.get("approvals_required", [])), set(gold.get("approvals_forbidden", []))
    req_f, forb_f = set(gold.get("flags_required", [])), set(gold.get("flags_forbidden", []))

    status_ok = status in gold["status"]
    approvals_ok = req_a <= approvals and not (approvals & forb_a)
    approvals_exact = approvals == req_a if req_a else not (approvals & forb_a)
    flags_ok = req_f <= flags and not (flags & forb_f)
    missing_ok = all(_contains_group(d.missing_information, g) for g in gold.get("missing_any_groups", []))
    if gold.get("max_missing_information") is not None:
        missing_ok = missing_ok and len(d.missing_information) <= gold["max_missing_information"]
    safe_language = not (_UNSAFE_LANGUAGE.search(d.recommendation) or _UNSAFE_LANGUAGE.search(d.next_step))
    human_ok = d.human_review_required is True
    passed = all([schema_valid, status_ok, approvals_ok, flags_ok, missing_ok, safe_language, human_ok])

    g = res.guardrails
    claims = g.grounded_claims + len(g.ungrounded_claims)
    llm_mode = res.mode == "llm"
    pub = case.get("public_case")
    public_pass = None
    if pub and pub in public_cases:
        public_pass = not public_evaluate(d, public_cases[pub]["expectations"])
    usage = res.usage
    tel = d.telemetry
    row.update(
        mode=res.mode, request_id=d.request_id, final_status=status, llm_status=g.llm_status or "",
        passed=passed, public_case=pub or "", public_pass="" if public_pass is None else public_pass,
        schema_valid=schema_valid, status_ok=status_ok, approvals_ok=approvals_ok, approvals_exact=approvals_exact,
        flags_ok=flags_ok, missing_ok=missing_ok, safe_language=safe_language, human_review_ok=human_ok,
        raw_llm_policy_compliant="" if not llm_mode else g.raw_policy_compliant,
        llm_omitted_approvals="|".join(g.omitted_approvals), llm_omitted_flags="|".join(g.omitted_flags),
        llm_extra_approvals="|".join(g.added_by_llm_approvals), llm_extra_flags="|".join(g.added_by_llm_flags),
        status_overridden=g.status_overridden, guardrail_corrections=len(g.corrections),
        agent_claims=claims, grounded_claims=g.grounded_claims, ungrounded_claims=len(g.ungrounded_claims),
        evidence_items=len(d.evidence), latency_ms=res.latency_ms,
        llm_calls=tel.llm_calls if tel else 0, tool_calls=tel.tool_calls if tel else 0,
        input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0), cost_usd=res.cost_usd or 0.0,
        approvals="|".join(d.required_approvals), risk_flags="|".join(d.risk_flags),
        fallback_reason=res.fallback_reason or "", recommendation=d.recommendation,
    )
    return row


def _rate(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) not in ("", None)]
    return round(100 * sum(bool(v) for v in vals) / len(vals), 1) if vals else None


def _avg(rows: list[dict], key: str) -> float | None:
    vals = [float(r[key]) for r in rows if r.get(key) not in ("", None)]
    return round(statistics.mean(vals), 2) if vals else None


def _pct(rows: list[dict], key: str, q: float) -> float | None:
    vals = sorted(float(r[key]) for r in rows if r.get(key) not in ("", None))
    if not vals:
        return None
    idx = min(len(vals) - 1, max(0, round(q * (len(vals) - 1))))
    return round(vals[idx], 1)


def summarize(rows: list[dict]) -> dict:
    by_arch: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_arch[r["architecture"]].append(r)
    summary: dict = {}
    for arch, rs in by_arch.items():
        ok = [r for r in rs if "passed" in r and "mode" in r]
        claims = sum(int(r.get("agent_claims") or 0) for r in ok)
        grounded = sum(int(r.get("grounded_claims") or 0) for r in ok)
        consistency = None
        reps = defaultdict(set)
        for r in ok:
            reps[r["case_id"]].add((r["final_status"], r["approvals"], r["risk_flags"]))
        if ok and max(r["repeat"] for r in ok) > 0:
            consistency = round(100 * sum(1 for v in reps.values() if len(v) == 1) / len(reps), 1)
        by_edge: dict[str, list] = defaultdict(list)
        for r in ok:
            by_edge[r["edge_case"]].append(r["passed"])
        summary[arch] = {
            "runs": len(rs), "errors": sum(1 for r in rs if r.get("error")),
            "modes": dict(sorted({m: sum(1 for r in ok if r["mode"] == m) for m in {r["mode"] for r in ok}}.items())),
            "pass_rate": _rate(ok, "passed"),
            "public_min_checks_pass": f"{sum(1 for r in ok if r.get('public_pass') is True)}/{sum(1 for r in ok if r.get('public_pass') in (True, False))}",
            "status_accuracy": _rate(ok, "status_ok"),
            "approvals_correct": _rate(ok, "approvals_ok"),
            "approvals_exact": _rate(ok, "approvals_exact"),
            "risk_flags_correct": _rate(ok, "flags_ok"),
            "missing_info_correct": _rate(ok, "missing_ok"),
            "safe_language": _rate(ok, "safe_language"),
            "human_review_correct": _rate(ok, "human_review_ok"),
            "raw_llm_policy_compliance": _rate(ok, "raw_llm_policy_compliant"),
            "llm_status_overridden": sum(1 for r in ok if r.get("status_overridden") is True),
            "guardrail_corrections": sum(int(r.get("guardrail_corrections") or 0) for r in ok),
            "agent_evidence_claims": claims,
            "grounding_rate": round(100 * grounded / claims, 1) if claims else None,
            "ungrounded_claims_dropped": claims - grounded,
            "avg_latency_ms": _avg(ok, "latency_ms"), "p50_latency_ms": _pct(ok, "latency_ms", 0.5),
            "p95_latency_ms": _pct(ok, "latency_ms", 0.95),
            "avg_llm_calls": _avg(ok, "llm_calls"), "avg_tool_calls": _avg(ok, "tool_calls"),
            "avg_input_tokens": _avg(ok, "input_tokens"), "avg_output_tokens": _avg(ok, "output_tokens"),
            "avg_cost_usd": _avg(ok, "cost_usd"),
            "consistency_across_repeats": consistency,
            "pass_rate_by_edge_case": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_edge.items())},
            "failed_cases": sorted({r["case_id"] for r in ok if not r["passed"]}),
        }
    return summary


def comparison_markdown(summary: dict, meta: dict) -> str:
    archs = [a for a in ("single", "staged", "rules") if a in summary]
    names = {"single": "A · Single agent", "staged": "B · Staged 2-agent", "rules": "Rules-only baseline"}
    rows = [
        ("Cases passing all quality checks", "pass_rate", "%"),
        ("Public minimum checks", "public_min_checks_pass", ""),
        ("Correct next action (status)", "status_accuracy", "%"),
        ("Required approvals correct", "approvals_correct", "%"),
        ("Approvals exact (no over-escalation)", "approvals_exact", "%"),
        ("Risk flags correct", "risk_flags_correct", "%"),
        ("Missing-info handling correct", "missing_info_correct", "%"),
        ("Human review correct", "human_review_correct", "%"),
        ("Raw LLM policy compliance (pre-guardrail)", "raw_llm_policy_compliance", "%"),
        ("Guardrail corrections (total)", "guardrail_corrections", ""),
        ("Agent evidence grounding rate", "grounding_rate", "%"),
        ("Ungrounded claims dropped", "ungrounded_claims_dropped", ""),
        ("Avg latency (ms)", "avg_latency_ms", ""),
        ("p95 latency (ms)", "p95_latency_ms", ""),
        ("Avg LLM calls", "avg_llm_calls", ""),
        ("Avg tool calls", "avg_tool_calls", ""),
        ("Avg input / output tokens", None, ""),
        ("Avg cost per request (USD)", "avg_cost_usd", ""),
        ("Consistency across repeats", "consistency_across_repeats", "%"),
        ("Execution mode", "modes", ""),
    ]
    out = [f"_Generated {meta['generated_at']} · {meta['cases']} cases × {meta['repeats']} repeat(s) · "
           f"provider `{meta['provider']}` · model `{meta['model'] or 'n/a'}`_", "",
           "| Metric | " + " | ".join(names[a] for a in archs) + " |", "|---|" + "---:|" * len(archs)]
    for label, key, unit in rows:
        cells = []
        for a in archs:
            s = summary[a]
            if key is None:
                v = f"{s['avg_input_tokens'] or 0:.0f} / {s['avg_output_tokens'] or 0:.0f}"
            else:
                v = s.get(key)
                if isinstance(v, dict):
                    v = ", ".join(f"{k}: {n}" for k, n in v.items())
                elif v is None:
                    v = "n/a"
                elif unit == "%":
                    v = f"{v}%"
            cells.append(str(v))
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    out.append("")
    for a in archs:
        if summary[a]["failed_cases"]:
            out.append(f"- **{names[a]}** failed: {', '.join(summary[a]['failed_cases'])}")
    out += ["", ship_decision(summary)]
    return "\n".join(out).strip() + "\n"


# Pre-registered before any LLM results were collected (see docs/ARCHITECTURE_DECISION.md):
# ship the simpler single agent unless the staged variant is better by more than run-to-run
# noise on end-to-end quality, without losing safety.
SHIP_MARGIN_PTS = 5.0


def ship_decision(summary: dict) -> str:
    a, b = summary.get("single"), summary.get("staged")
    if not a or not b:
        return "**Decision rule:** needs both `single` and `staged` results."
    if a["modes"].get("llm", 0) == 0 or b["modes"].get("llm", 0) == 0:
        return ("**Decision rule:** pending - single/staged ran in deterministic fallback mode (no LLM key), "
                "so they are identical by construction. Run with an API key to compare the architectures.")
    safe = lambda s: (s["safe_language"] or 0) == 100 and (s["human_review_correct"] or 0) == 100 and s["errors"] == 0  # noqa: E731
    gain = (b["pass_rate"] or 0) - (a["pass_rate"] or 0)
    lat = ((b["avg_latency_ms"] or 0) / (a["avg_latency_ms"] or 1) - 1) * 100
    calls = (b["avg_llm_calls"] or 0) - (a["avg_llm_calls"] or 0)
    if not safe(a) and safe(b):
        choice, why = "B · Staged 2-agent", "A failed a safety criterion that B met"
    elif safe(b) and gain >= SHIP_MARGIN_PTS:
        choice, why = "B · Staged 2-agent", f"B passes {gain:+.1f} pts more cases (threshold +{SHIP_MARGIN_PTS:.0f})"
    else:
        speed = f"{abs(lat):.0f}% {'slower' if lat >= 0 else 'faster'}"
        choice, why = "A · Single agent", (f"B's pass rate differs by {gain:+.1f} pts (threshold +{SHIP_MARGIN_PTS:.0f}); "
                                            f"B was {speed} with {calls:+.1f} LLM calls per request, which does not offset the quality gap")
    return f"**Pre-registered decision rule → ship {choice}:** {why}."


def compact_markdown(summary: dict, meta: dict) -> str:
    """Short table for the 500-word decision memo."""
    archs = [a for a in ("single", "staged", "rules") if a in summary]
    names = {"single": "Single (A)", "staged": "Staged (B)", "rules": "Rules only"}
    rows = [("Cases passing quality criteria", "pass_rate", "%"),
            ("Evidence grounding rate", "grounding_rate", "%"), ("Avg latency (s)", "avg_latency_ms", "s"),
            ("Avg LLM calls", "avg_llm_calls", "")]
    out = [f"_{meta['cases']} cases x {meta['repeats']} repeat(s), {meta['provider']} {meta['model'] or ''}_", "",
           "| Metric | " + " | ".join(names[a] for a in archs) + " |", "|---|" + "---:|" * len(archs)]
    for label, key, unit in rows:
        cells = []
        for a in archs:
            v = summary[a].get(key)
            cells.append("n/a" if v is None else (f"{v}%" if unit == "%" else f"{v / 1000:.1f}" if unit == "s" else str(v)))
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    out += ["", ship_decision(summary)]
    return "\n".join(out).strip() + "\n"


def update_docs(markdown: str, compact: str) -> list[str]:
    changed = []
    for path, block in [(ROOT / "README.md", markdown), (ROOT / "docs" / "ARCHITECTURE_DECISION.md", compact)]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if MARK_START in text and MARK_END in text:
            head, rest = text.split(MARK_START, 1)
            _, tail = rest.split(MARK_END, 1)
            path.write_text(f"{head}{MARK_START}\n{block}{MARK_END}{tail}", encoding="utf-8")
            changed.append(str(path.relative_to(ROOT)))
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--architectures", nargs="+", default=["single", "staged", "rules"], choices=["single", "staged", "rules"])
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--cases", nargs="*", help="Only run these case ids")
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--update-docs", action="store_true")
    ap.add_argument("--no-traces", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="Ignore the checkpoint and re-run everything")
    ap.add_argument("--retry-fallbacks", action="store_true",
                    help="On resume, re-run single/staged runs that fell back to rules (e.g. after rate limits)")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    settings = get_settings()
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if args.cases:
        cases = [c for c in cases if c["case_id"] in set(args.cases)]
    public = {c["case_id"]: c for c in json.loads(PUBLIC_PATH.read_text(encoding="utf-8"))}
    if not settings.llm_enabled and any(a != "rules" for a in args.architectures):
        print("WARNING: no LLM provider configured - single/staged will run in deterministic fallback mode.\n"
              "         Set ANTHROPIC_API_KEY (or OPENAI_API_KEY) in .env for the real comparison.\n")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Checkpoint: every finished run is appended immediately, so an interrupted evaluation
    # (free-tier rate limits can make it slow) resumes instead of starting over.
    checkpoint = out / "checkpoint.jsonl"
    done: dict[tuple, dict] = {}
    if checkpoint.exists() and not args.fresh:
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if args.retry_fallbacks and row.get("mode") == "rules_fallback":
                continue
            done[(row["case_id"], row["architecture"], row["repeat"])] = row
    elif checkpoint.exists():
        checkpoint.unlink()

    all_jobs = [(c, a, r) for r in range(args.repeats) for a in args.architectures for c in cases]
    jobs = [j for j in all_jobs if (j[0]["case_id"], j[1], j[2]) not in done]
    print(f"Running {len(jobs)} of {len(all_jobs)} runs ({len(cases)} cases × {len(args.architectures)} architectures × "
          f"{args.repeats} repeats; {len(done)} already in checkpoint) provider={settings.llm_provider} "
          f"model={settings.model_name or 'n/a'}\n", flush=True)
    finished = len(done)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool, checkpoint.open("a", encoding="utf-8") as ck:
        futures = [pool.submit(run_one, *j) for j in jobs]
        for fut in as_completed(futures):
            run = fut.result()
            row = score(run, public)
            done[(row["case_id"], row["architecture"], row["repeat"])] = row
            ck.write(json.dumps(row, default=str) + "\n")
            ck.flush()
            if not args.no_traces and run["result"] is not None:
                tdir = out / "traces" / run["architecture"]
                tdir.mkdir(parents=True, exist_ok=True)
                payload = {"case_id": run["case"]["case_id"], "decision": run["result"].decision.model_dump(),
                           "trace": run["result"].trace()}
                (tdir / f"{run['case']['case_id']}_r{run['repeat']}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            finished += 1
            mark = "PASS" if row.get("passed") else ("ERR " if run["error"] else "FAIL")
            print(f"[{finished}/{len(all_jobs)}] {mark} {row['architecture']:<6} {row['case_id']}  r{row['repeat']}  "
                  f"{row.get('final_status', '-'):<27} {row.get('latency_ms', 0):>8.0f} ms  llm={row.get('llm_calls', '-')} "
                  f"tools={row.get('tool_calls', '-')}  mode={row.get('mode', '-')}"
                  + (f"  [{run['error']}]" if run["error"] else ""), flush=True)

    wanted = {(c["case_id"], a, r) for c, a, r in all_jobs}
    rows = sorted((row for key, row in done.items() if key in wanted), key=lambda r: (r["architecture"], r["case_id"], r["repeat"]))
    summary = summarize(rows)
    meta = {"generated_at": time.strftime("%Y-%m-%d %H:%M"), "cases": len(cases), "repeats": args.repeats,
            "provider": settings.llm_provider, "model": settings.model_name, "effort": settings.llm_effort}
    for arch in args.architectures:
        arch_rows = [r for r in rows if r["architecture"] == arch]
        if arch_rows:
            keys = list(dict.fromkeys(k for r in arch_rows for k in r))
            with (out / f"runs_{arch}.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                w.writerows(arch_rows)
    with (out / "evaluation_results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "architecture", "correct_next_action", "grounded_evidence", "policy_followed",
                    "human_escalation_correct", "latency_ms", "llm_calls", "tool_calls", "notes"])
        for r in rows:
            if "mode" not in r:
                w.writerow([r["case_id"], r["architecture"], False, False, False, False, "", "", "", r["error"]])
                continue
            w.writerow([r["case_id"], r["architecture"], r["status_ok"], r["ungrounded_claims"] == 0,
                        r["approvals_ok"] and r["flags_ok"] and r["missing_ok"], r["human_review_ok"] and r["status_ok"],
                        r["latency_ms"], r["llm_calls"], r["tool_calls"],
                        f"r{r['repeat']} mode={r['mode']} status={r['final_status']}"
                        + (f" corrections={r['guardrail_corrections']}" if r["guardrail_corrections"] else "")])
    (out / "summary.json").write_text(json.dumps({"meta": meta, "summary": summary}, indent=2), encoding="utf-8")
    md = comparison_markdown(summary, meta)
    (out / "comparison.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"Results written to {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    if args.update_docs:
        changed = update_docs(md, compact_markdown(summary, meta))
        print("Updated docs: " + (", ".join(changed) or "none (markers not found)"))


if __name__ == "__main__":
    main()
