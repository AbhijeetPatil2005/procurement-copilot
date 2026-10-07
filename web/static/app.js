"use strict";
/* Procurement Copilot - single-page reviewer UI (no framework, no build step).
   All business data is untrusted: every value rendered into HTML goes through esc(). */

const STATUS = {
  ROUTE_FOR_APPROVAL: { c: "#059669", i: "✓", why: "Standard business approvals only" },
  ESCALATE_SPECIALIST_REVIEW: { c: "#d97706", i: "⚑", why: "Specialist reviews must clear before business sign-off" },
  REDIRECT_TO_EXISTING_TOOL: { c: "#2563eb", i: "↺", why: "An approved tool may already meet this need" },
  MANUAL_REVIEW_REQUIRED: { c: "#dc2626", i: "!", why: "Evidence is missing, conflicting or unverifiable" },
  REQUEST_CLARIFICATION: { c: "#7c3aed", i: "?", why: "The request is incomplete or unclear" },
};
const SHORT = {
  ROUTE_FOR_APPROVAL: "Route", ESCALATE_SPECIALIST_REVIEW: "Escalate", REDIRECT_TO_EXISTING_TOOL: "Existing tool",
  MANUAL_REVIEW_REQUIRED: "Manual review", REQUEST_CLARIFICATION: "Clarify",
};
const FLAGS = {
  budget_insufficient: ["critical", "Cost exceeds the department's available budget"],
  budget_unverifiable: ["critical", "No budget record exists for the department"],
  vendor_review_expired: ["critical", "Vendor security assessment is older than 365 days"],
  conflicting_vendor_evidence: ["critical", "Vendor registry and risk service disagree"],
  vendor_risk_unavailable: ["critical", "Vendor-risk service could not be verified"],
  prompt_injection_detected: ["critical", "Instructions hidden in business data were ignored"],
  ai_assessment_unavailable: ["critical", "AI unavailable - deterministic policy result only"],
  security_review_required: ["review", "Security must review before approval"],
  privacy_review_required: ["review", "Personal data or out-of-region storage involved"],
  legal_review_required: ["review", "New vendor ≥ $10k, non-standard terms or cross-region data"],
  restricted_use_scope: ["review", "Existing approval does not cover this data use"],
  missing_information: ["review", "Required request details are missing"],
  existing_tool_overlap: ["info", "An approved tool may already cover this need"],
};
const SEV = { critical: ["#dc2626", "Critical"], review: ["#d97706", "Review"], info: ["#2563eb", "Note"] };
const EVIDENCE = [
  ["get_purchase_request", "Request", "🧾"], ["get_requester_profile", "Requester", "👤"], ["check_department_budget", "Budget", "💰"],
  ["search_existing_software", "Existing software", "🧩"], ["get_vendor_registry_record", "Vendor registry", "🏢"],
  ["get_vendor_risk_assessment", "Vendor-risk service", "🛡️"], ["evaluate_procurement_policy", "Policy rules", "⚖️"],
  ["lookup_policy_section", "Policy text", "📘"],
];
const SPECIALISTS = new Set(["Security", "Privacy", "Legal"]);
const ARCH = { single: "A · Single agent", staged: "B · Staged", rules: "Rules only" };
const WORKFLOW = ["Request", "Understand", "Gather evidence", "Recommend", "Human review"];

const state = { meta: null, requests: [], selected: null, arch: "single", results: {}, running: false, sideTab: "evidence", action: "accept_routing" };

// ---------------------------------------------------------------- helpers
const $ = (s) => document.querySelector(s);
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}
function money(v) {
  if (v === null || v === undefined || v === "") return "Not provided";
  return "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });
}
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) {}
    throw new Error(msg);
  }
  return res.headers.get("content-type")?.includes("json") ? res.json() : res.text();
}
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 3200);
}
const key = (rid, arch) => `${rid}::${arch}`;
const current = () => state.requests.find((r) => r.request_id === state.selected);
const result = () => state.results[key(state.selected, state.arch)];
function statusBadge(status, label) {
  const s = STATUS[status];
  return s ? `<span class="badge" style="--c:${s.c}">${s.i} ${esc(label || SHORT[status])}</span>` : `<span class="badge new">Not analyzed</span>`;
}

// ---------------------------------------------------------------- top bar
function renderStatus() {
  const m = state.meta;
  $("#status").innerHTML =
    `<span class="pill"><span class="dot" style="background:${m.vendor_api_online ? "#34d399" : "#f87171"}"></span>Vendor API ${m.vendor_api_online ? "online" : "offline"}</span>` +
    `<span class="pill">${m.llm_enabled ? "🤖 " + esc(m.model) : "⚙️ No LLM key"}</span>` +
    `<span class="pill">📅 ${esc(m.policy_reference_date)}</span>`;
}

// ---------------------------------------------------------------- inbox
function renderInbox() {
  const q = $("#search").value.trim().toLowerCase();
  const items = state.requests.filter((r) => !q || JSON.stringify([r.request_id, r.product_name, r.vendor_name, r.department]).toLowerCase().includes(q));
  $("#inbox-count").textContent = `${state.requests.length} requests`;
  $("#inbox-list").innerHTML = items.map((r) => {
    const last = r.last_result;
    return `<button class="req ${r.request_id === state.selected ? "active" : ""}" data-id="${esc(r.request_id)}">
      <div class="req-top"><span class="req-id">${esc(r.request_id)}</span>${statusBadge(last?.status, last ? SHORT[last.status] : null)}</div>
      <div class="req-name">${esc(r.product_name || "(no product)")}</div>
      <div class="req-meta">${esc(r.department || "Unknown dept")} · ${esc(money(r.annual_cost_usd))}</div>
    </button>`;
  }).join("") || `<div class="empty-side">No matching requests</div>`;
}

// ---------------------------------------------------------------- case view
function stepper(phase) {
  // phase: idle | running-N | done
  return `<div class="steps">${WORKFLOW.map((name, i) => {
    const n = i + 1;
    let cls = "";
    if (phase === "done") cls = n < 5 ? "done" : "human";
    else if (phase.startsWith("running")) { const a = Number(phase.split("-")[1]); cls = n < a ? "done" : n === a ? "active" : ""; }
    else cls = n === 1 ? "done" : n === 2 ? "active" : "";
    return `<div class="step ${cls}"><span class="n">${cls === "done" ? "✓" : n}</span><span>${name}</span></div>`;
  }).join("")}</div>`;
}

function requestBlock(r, res) {
  const lvl = String(r.data_access_level || "unknown");
  const dataCls = /pii|source|confidential|credential/.test(lvl) ? "bad" : lvl === "unknown" ? "warn" : "";
  let budget = `<div class="v">—</div>`;
  const b = res?.assessment?.budget;
  if (b) {
    if (b.within_budget === true) budget = `<div class="v good">Within · ${esc(money(b.remaining_after_usd))} left</div>`;
    else if (b.within_budget === false) budget = `<div class="v bad">Short ${esc(money(-b.remaining_after_usd))}</div>`;
    else budget = `<div class="v warn">Can't verify</div>`;
  }
  const inj = res?.assessment?.injection;
  const injHtml = inj?.detected
    ? `<div class="alert"><b>Embedded instructions detected and ignored:</b> ${inj.findings.slice(0, 3).map((f) => `“${esc(f.excerpt)}”`).join(" · ")}</div>` : "";
  return `
    <div class="tiles" style="margin-top:16px">
      <div class="tile"><div class="k">Annual cost</div><div class="v ${r.annual_cost_usd == null ? "warn" : ""}">${esc(money(r.annual_cost_usd))}</div></div>
      <div class="tile"><div class="k">Users</div><div class="v ${r.user_count == null ? "warn" : ""}">${esc(r.user_count ?? "Not provided")}</div></div>
      <div class="tile"><div class="k">Budget fit</div>${budget}</div>
      <div class="tile"><div class="k">Data access</div><div class="v ${dataCls}">${esc(lvl.replace(/_/g, " "))}</div></div>
      <div class="tile"><div class="k">Integrations</div><div class="v">${esc((r.requested_integrations || []).join(", ") || "None")}</div></div>
    </div>
    <div class="just"><div class="lbl">Business justification · untrusted text, treated as data</div>${esc(r.business_justification || "(empty)")}</div>
    ${injHtml}`;
}

function decisionBlock(res) {
  const s = STATUS[res.status] || { c: "#64748b", i: "•", why: "" };
  const d = res.decision;
  let body = d.recommendation;
  if (body.startsWith(res.status_label + ":")) body = body.slice(res.status_label.length + 1).trim();
  body = body.charAt(0).toUpperCase() + body.slice(1);
  const t = d.telemetry || {};
  const g = res.guardrails;
  const mode = { llm: "AI + rules", rules_only: "Rules only", rules_fallback: "Rules (AI fallback)" }[res.mode] || res.mode;
  return `
    <div class="decision" style="--c:${s.c}">
      <div class="decision-head"><div class="decision-ico">${s.i}</div>
        <div><div class="decision-label">${esc(res.status_label)}</div><div class="decision-why">${esc(s.why)}</div></div></div>
      <div class="decision-body">${esc(body)}</div>
      <div class="next"><div class="lbl">Next step</div>${esc(d.next_step)}</div>
      <div class="metrics">
        <div class="metric"><div class="k">Latency</div><div class="v">${(res.latency_ms / 1000).toFixed(1)}s</div><div class="s">${esc(mode)}</div></div>
        <div class="metric"><div class="k">LLM calls</div><div class="v">${t.llm_calls ?? 0}</div><div class="s">${esc(res.architecture_label)}</div></div>
        <div class="metric"><div class="k">Tool calls</div><div class="v">${t.tool_calls ?? 0}</div><div class="s">incl. policy engine</div></div>
        <div class="metric"><div class="k">Guardrail fixes</div><div class="v">${g.corrections.length}</div><div class="s">${g.grounded_claims} AI claims grounded</div></div>
      </div>
      <div class="advisory">Advisory only. The copilot cannot purchase, approve spend, change budgets or waive reviews.</div>
    </div>
    ${res.mode === "rules_fallback" ? `<div class="warnbox">AI assessment unavailable, so this is the deterministic policy result. ${esc(res.fallback_reason || "")}</div>` : ""}`;
}

function approvalsBlock(res) {
  const routes = Object.fromEntries((res.assessment?.approver_routing || []).map((r) => [r.role, r.assignee]));
  const appr = res.decision.required_approvals;
  const row = (a) => `<div class="appr"><span class="chk"></span><div><div class="role">${esc(a)}</div><div class="who">${esc(routes[a] || "Assign reviewer")}</div></div></div>`;
  const spec = appr.filter((a) => SPECIALISTS.has(a)), biz = appr.filter((a) => !SPECIALISTS.has(a));
  let html = "";
  if (spec.length) html += `<div class="group">1 · Specialist reviews</div>${spec.map(row).join("")}<div class="group">2 · Business approvals</div>`;
  html += biz.map(row).join("");
  return `<div class="card"><div class="card-title">Approvals required <span>${appr.length}</span></div>${html}</div>`;
}

function flagsBlock(res) {
  const order = { critical: 0, review: 1, info: 2 };
  const flags = [...res.decision.risk_flags].sort((a, b) => order[(FLAGS[a] || ["info"])[0]] - order[(FLAGS[b] || ["info"])[0]]);
  const html = flags.map((f) => {
    const [sev, desc] = FLAGS[f] || ["info", "Additional risk raised by the AI reviewer"];
    const [c, name] = SEV[sev];
    return `<div class="flag"><span class="sev" style="--c:${c}">${name}</span><div><div class="name">${esc(f.replace(/_/g, " "))}</div><div class="desc">${esc(desc)}</div></div></div>`;
  }).join("");
  return `<div class="card"><div class="card-title">Risk flags <span>${flags.length}</span></div>${html || `<div class="ok-line">✓ No risk flags</div>`}</div>`;
}

function listCard(title, items) {
  if (!items?.length) return "";
  return `<div class="card"><div class="card-title">${esc(title)} <span>${items.length}</span></div><ul class="list">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>`;
}

function humanBlock(res) {
  const actions = state.meta.human_actions;
  return `<div class="card">
    <div class="card-title">Human decision</div>
    <div class="actions">${Object.entries(actions).map(([k, v]) =>
      `<button class="act ${state.action === k ? "active" : ""}" data-action="${esc(k)}" title="${esc(v)}">${esc(v.split(" - ")[0].split(" (")[0])}</button>`).join("")}</div>
    <div class="form-row" style="margin-top:10px">
      <label>Reviewer name<input id="h-reviewer" placeholder="e.g. Priya Shah" maxlength="120"></label>
      <label>Justification / notes<input id="h-just" placeholder="Required for override or reject" maxlength="2000"></label>
    </div>
    <div class="actions" style="justify-content:space-between;align-items:center">
      <span class="muted small">Saved to the append-only audit log. The copilot never approves.</span>
      <span style="display:flex;gap:8px">
        <a class="btn btn-ghost btn-sm" href="/api/packet/${encodeURIComponent(res.request_id)}/${encodeURIComponent(res.architecture)}">⬇ Review packet</a>
        <button class="btn btn-primary btn-sm" id="h-submit">Record decision</button>
      </span>
    </div>
  </div>`;
}

function renderCase() {
  const r = current();
  const el = $("#case");
  if (!r) { el.innerHTML = `<div class="empty"><div class="big">📥</div><div class="t">Select a request</div></div>`; return; }
  const res = result();
  const phase = state.running ? `running-${state.runStep || 2}` : res ? "done" : "idle";
  const seg = Object.entries(ARCH).map(([k, v]) => `<button data-arch="${k}" class="${state.arch === k ? "active" : ""}">${v}</button>`).join("");
  let html = `
    <div class="case-head">
      <div style="min-width:0">
        <div class="case-id">${esc(r.request_id)} · ${esc(r.requester_name || r.requester_id)} · ${esc(r.department || "Unknown dept")}</div>
        <div class="case-title">${esc(r.product_name || "(no product)")}</div>
        <div class="case-sub">${esc(r.vendor_name || "(no vendor)")}</div>
        <div class="tags"><span class="tag">${esc(r.category || "Uncategorised")}</span><span class="tag ${r.urgency !== "normal" ? "warn" : "grey"}">Urgency: ${esc(r.urgency)}</span></div>
      </div>
      <div class="runbar"><div class="seg" id="seg">${seg}</div>
        <button class="btn btn-primary" id="run" ${state.running ? "disabled" : ""}>${state.running ? `<span class="spinner"></span>Analyzing…` : res ? "Re-analyze" : "Analyze"}</button></div>
    </div>
    ${stepper(phase)}
    ${requestBlock(r, res)}`;
  if (state.running) {
    html += `<div class="skeleton" style="height:190px;margin-top:14px"></div><div class="grid2" style="margin-top:14px"><div class="skeleton" style="height:180px"></div><div class="skeleton" style="height:180px"></div></div>`;
  } else if (res) {
    html += decisionBlock(res);
    html += `<div class="grid2" style="margin-top:14px">${approvalsBlock(res)}${flagsBlock(res)}</div>`;
    const extra = listCard("Missing information", res.decision.missing_information) + listCard("Open items for reviewers", res.assessment?.open_items);
    if (extra) html += `<div style="margin-top:14px">${extra}</div>`;
    html += `<div style="margin-top:14px">${humanBlock(res)}</div>`;
  } else {
    html += `<div class="card" style="margin-top:14px;text-align:center;padding:32px">
      <div style="font-size:30px">🧭</div><div style="font-weight:800;font-size:16px;margin-top:4px">Ready to analyze</div>
      <div class="muted" style="max-width:460px;margin:4px auto 0">Pick an architecture and click <b>Analyze</b>. The copilot gathers evidence with its tools,
      runs the deterministic policy engine and prepares a recommendation for human review.</div></div>`;
  }
  el.innerHTML = html;
}

// ---------------------------------------------------------------- side panel
function renderSide() {
  document.querySelectorAll(".side-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.sideTab));
  const res = result();
  const body = $("#side-body");
  if (!res) { body.innerHTML = `<div class="empty-side">${state.running ? "Gathering evidence…" : "Run an analysis to see the evidence gathered by the tools."}</div>`; return; }
  if (state.sideTab === "evidence") {
    const groups = {};
    res.decision.evidence.forEach((e) => { const k = e.source.replace(" (agent)", ""); (groups[k] ||= []).push(e); });
    body.innerHTML = `<div class="muted small" style="margin-bottom:10px">${res.decision.evidence.length} findings rendered from tool outputs. <span class="ai-tag">AI</span> marks model interpretations that passed the grounding check.</div>` +
      EVIDENCE.filter(([k]) => groups[k]).map(([k, title, icon], i) => `
      <details class="evg" ${i < 3 ? "open" : ""}><summary><span>${icon}</span>${title}<span class="cnt">${groups[k].length}</span><span class="chev">›</span></summary>
        ${groups[k].map((e) => { const ai = e.source.endsWith("(agent)");
          return `<div class="ev ${ai ? "ai" : ""}">${esc(e.finding)}${ai ? `<span class="ai-tag">AI</span>` : ""}<div class="ref">${esc(e.reference || k)}</div></div>`; }).join("")}
      </details>`).join("") +
      (res.guardrails.ungrounded_claims.length ? `<div class="warnbox">${res.guardrails.ungrounded_claims.length} AI claim(s) dropped as ungrounded.</div>` : "");
  } else {
    const g = res.guardrails, a = res.assessment;
    const tools = res.tool_log.filter((c) => !(c.caller === "system" && c.cached));
    body.innerHTML = `
      <div class="trace-sec"><h4>Run</h4>
        <div class="kv"><span>Architecture</span><b>${esc(res.architecture_label)}</b></div>
        <div class="kv"><span>Mode</span><b>${esc(res.mode)}</b></div>
        <div class="kv"><span>Model</span><b>${esc(res.model || "none")}</b></div>
        <div class="kv"><span>Tokens in / out</span><b>${res.usage.input_tokens || 0} / ${res.usage.output_tokens || 0}</b></div>
        <div class="kv"><span>AI draft status</span><b>${esc(g.llm_status || "n/a")}</b></div>
        <div class="kv"><span>Raw draft policy-compliant</span><b>${res.mode === "llm" ? (g.raw_policy_compliant ? "yes" : "no") : "n/a"}</b></div>
      </div>
      <div class="trace-sec"><h4>Guardrail corrections (${g.corrections.length})</h4>
        ${g.corrections.length ? `<ul class="list small">${g.corrections.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : `<div class="ok-line small">✓ None needed</div>`}</div>
      <div class="trace-sec"><h4>Tool calls (${tools.length})</h4><div class="tl">${tools.map((c) =>
        `<div class="tl-item" style="--c:${c.ok ? "#4f46e5" : "#dc2626"}"><b class="mono">${esc(c.name)}</b> <span class="muted">· ${esc(c.caller)}${c.cached ? " · cached" : ""} · ${c.latency_ms} ms</span></div>`).join("")}</div></div>
      ${a ? `<div class="trace-sec"><h4>Policy rules applied (${a.rule_hits.length})</h4>${a.rule_hits.map((h) =>
        `<div class="rule"><span class="sec">${esc(h.section)}</span>${esc(h.finding)}</div>`).join("")}</div>` : ""}
      ${res.evidence_pack ? `<div class="trace-sec"><h4>Analyst → Reviewer handoff</h4><div class="small">${esc(res.evidence_pack.need_summary)}</div>
        ${res.evidence_pack.open_questions.length ? `<div class="small muted" style="margin-top:4px">Open questions: ${esc(res.evidence_pack.open_questions.join(" | "))}</div>` : ""}</div>` : ""}`;
  }
}

// ---------------------------------------------------------------- actions
async function runAnalysis() {
  const rid = state.selected, arch = state.arch;
  state.running = true; state.runStep = 2; renderCase(); renderSide();
  const timer = setInterval(() => { if (state.runStep < 4) { state.runStep++; renderCase(); } }, 4000);
  try {
    const res = await api("/api/analyze", { method: "POST", body: JSON.stringify({ request_id: rid, architecture: arch }) });
    state.results[key(rid, arch)] = res;
    const r = state.requests.find((x) => x.request_id === rid);
    if (r) r.last_result = { status: res.status, status_label: res.status_label, architecture: arch };
  } catch (e) { toast("Analysis failed: " + e.message, true); }
  finally { clearInterval(timer); state.running = false; renderAll(); }
}

async function recordDecision() {
  const res = result();
  try {
    await api("/api/decisions", { method: "POST", body: JSON.stringify({
      request_id: res.request_id, architecture: res.architecture, reviewer: $("#h-reviewer").value,
      action: state.action, justification: $("#h-just").value }) });
    toast("Decision recorded in the audit log");
    $("#h-reviewer").value = ""; $("#h-just").value = "";
  } catch (e) { toast(e.message, true); }
}

function select(rid) { state.selected = rid; renderAll(); }
function renderAll() { renderInbox(); renderCase(); renderSide(); }

// ---------------------------------------------------------------- other views
async function renderCompare() {
  const r = current();
  const el = $("#compare");
  el.innerHTML = `<div class="page-head" style="display:flex;justify-content:space-between;align-items:flex-end;gap:16px">
      <div><div class="page-title">Compare architectures</div><div class="muted">Same request, three ways: ${esc(r?.request_id)} · ${esc(r?.product_name)}. Differences are highlighted.</div></div>
      <button class="btn btn-primary" id="cmp-run">Run comparison</button></div><div id="cmp-body"></div>`;
  const draw = (data) => {
    const all = Object.values(data);
    const sets = (f) => all.map((x) => JSON.stringify([...f(x)].sort()));
    const apprDiff = new Set(sets((x) => x.decision.required_approvals)).size > 1;
    const flagDiff = new Set(sets((x) => x.decision.risk_flags.filter((f) => f !== "ai_assessment_unavailable"))).size > 1;
    const stDiff = new Set(all.map((x) => x.status)).size > 1;
    $("#cmp-body").innerHTML = `<div class="cols3">${Object.entries(data).map(([arch, x]) => { const s = STATUS[x.status] || {};
      return `<div class="card cmp" style="--c:${s.c}">
        <div class="card-title" style="margin-bottom:0">${esc(x.architecture_label)}</div>
        <div class="st">${s.i} ${esc(x.status_label)}${stDiff ? `<span class="diff">differs</span>` : ""}</div>
        <div class="sec-lbl">Approvals${apprDiff ? `<span class="diff">differs</span>` : ""}</div><div>${esc(x.decision.required_approvals.join(", "))}</div>
        <div class="sec-lbl">Risk flags${flagDiff ? `<span class="diff">differs</span>` : ""}</div><div>${esc(x.decision.risk_flags.filter((f) => f !== "ai_assessment_unavailable").map((f) => f.replace(/_/g, " ")).join(", ") || "none")}</div>
        <div class="sec-lbl">Run</div><div>${(x.latency_ms / 1000).toFixed(1)}s · ${x.decision.telemetry.llm_calls} LLM · ${x.decision.telemetry.tool_calls} tools · ${esc(x.mode)}</div>
        <div class="sec-lbl">Guardrail corrections</div><div>${x.guardrails.corrections.length}</div>
        <div class="next" style="margin-top:12px"><div class="lbl">Next step</div>${esc(x.decision.next_step)}</div>
      </div>`; }).join("")}</div>`;
  };
  $("#cmp-run").onclick = async () => {
    $("#cmp-run").disabled = true; $("#cmp-run").innerHTML = `<span class="spinner"></span>Running…`;
    $("#cmp-body").innerHTML = `<div class="cols3"><div class="skeleton" style="height:320px"></div><div class="skeleton" style="height:320px"></div><div class="skeleton" style="height:320px"></div></div>`;
    try {
      const data = await api("/api/compare", { method: "POST", body: JSON.stringify({ request_id: r.request_id }) });
      Object.entries(data).forEach(([a, x]) => (state.results[key(r.request_id, a)] = x));
      draw(data);
    } catch (e) { toast(e.message, true); $("#cmp-body").innerHTML = ""; }
    finally { $("#cmp-run").disabled = false; $("#cmp-run").textContent = "Run comparison"; }
  };
  const cached = Object.fromEntries(Object.keys(ARCH).map((a) => [a, state.results[key(r?.request_id, a)]]).filter(([, v]) => v));
  if (Object.keys(cached).length === 3) draw(cached);
}

async function renderEval() {
  const el = $("#eval");
  const data = await api("/api/evaluation");
  if (!data.available) { el.innerHTML = `<div class="empty"><div class="t">No results yet</div><div>Run <span class="mono">python evals/run_eval_suite.py</span></div></div>`; return; }
  const archs = ["single", "staged", "rules"].filter((a) => data.summary[a]);
  const best = archs.reduce((b, a) => ((data.summary[a].pass_rate || 0) > (data.summary[b].pass_rate || 0) ? a : b));
  const f = (v, u = "") => (v === null || v === undefined ? "n/a" : v + u);
  const name = { single: "A · Single agent", staged: "B · Staged 2-agent", rules: "Rules-only baseline" };
  el.innerHTML = `
    <div class="page-head"><div class="page-title">Evaluation</div><div class="muted">${data.meta.cases} cases × ${data.meta.repeats} repeat(s) · same test set for every architecture · model ${esc(data.meta.model || "n/a")} · ${esc(data.meta.generated_at)}</div></div>
    <div class="win"><div class="t">🏁 ${esc(data.decision_line)}</div><div class="d">Rule fixed before any LLM results were collected: ship the simpler single agent unless the staged variant passes ≥5 points more cases without failing a safety check.</div></div>
    <div class="cols3">${archs.map((a) => { const s = data.summary[a]; return `
      <div class="card arch-card ${a === best ? "best" : ""}">
        <div class="card-title" style="margin-bottom:2px">${name[a]}${a === best ? " · 🏆" : ""}</div>
        <div class="big">${f(s.pass_rate, "%")}</div><div class="muted small" style="margin-bottom:8px">cases passing all quality checks</div>
        <div class="kv"><span>Public checks</span><b>${esc(s.public_min_checks_pass)}</b></div>
        <div class="kv"><span>Correct next action</span><b>${f(s.status_accuracy, "%")}</b></div>
        <div class="kv"><span>Approvals / flags correct</span><b>${f(s.approvals_correct, "%")} / ${f(s.risk_flags_correct, "%")}</b></div>
        <div class="kv"><span>Evidence grounding</span><b>${f(s.grounding_rate, "%")}</b></div>
        <div class="kv"><span>Avg latency</span><b>${((s.avg_latency_ms || 0) / 1000).toFixed(1)}s</b></div>
        <div class="kv"><span>Avg LLM / tool calls</span><b>${f(s.avg_llm_calls)} / ${f(s.avg_tool_calls)}</b></div>
        <div class="kv"><span>Failed</span><b style="text-align:right">${esc((s.failed_cases || []).join(", ") || "none")}</b></div>
      </div>`; }).join("")}</div>
    <div class="card-title" style="margin:22px 0 8px">Per-case results</div>
    <div class="table-wrap"><table class="tbl"><thead><tr><th>Case</th><th>Edge case</th><th>Scenario</th>${archs.map((a) => `<th>${name[a]}</th>`).join("")}</tr></thead><tbody>
      ${data.cases.map((c) => `<tr><td class="mono">${esc(c.case_id)}</td><td>${esc(c.edge_case.replace(/_/g, " "))}</td><td>${esc(c.title)}</td>${archs.map((a) => { const x = c.results[a];
        return `<td class="${x?.passed ? "cell-pass" : "cell-fail"}">${x ? (x.passed ? "✓ pass" : "✕ fail") : "—"}<div class="muted small" style="font-weight:500">${esc(SHORT[x?.status] || "")}</div></td>`; }).join("")}</tr>`).join("")}
    </tbody></table></div>`;
}

async function renderAudit() {
  const rows = await api("/api/decisions");
  $("#audit").innerHTML = `<div class="page-head"><div class="page-title">Audit log</div><div class="muted">Append-only record of human reviewer actions. The copilot itself never approves anything.</div></div>` +
    (rows.length ? `<div class="table-wrap"><table class="tbl"><thead><tr><th>Time (UTC)</th><th>Request</th><th>Reviewer</th><th>Action</th><th>Justification</th><th>Copilot recommendation</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td class="mono small">${esc(r.timestamp_utc)}</td><td>${esc(r.request_id)}</td><td>${esc(r.reviewer)}</td><td>${esc(r.action.replace(/_/g, " "))}</td><td>${esc(r.justification)}</td><td class="small">${esc(r.copilot_recommendation)}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="card empty"><div class="big">📜</div><div class="t">No decisions yet</div><div>Analyze a request and record a human decision.</div></div>`);
}

function renderHow() {
  $("#how").innerHTML = `
    <div class="page-head"><div class="page-title">How it works</div><div class="muted">AI interprets and recommends · Code decides thresholds and deterministic checks · Humans approve.</div></div>
    <div class="cols3">
      <div class="card layer" style="--c:#7c3aed"><div class="card-title">🤖 AI</div><b>Interprets context & recommends</b><div class="muted small" style="margin-top:4px">Is the stated gap vs existing tools credible? Does the text reveal data the form didn't declare? What should the requester be asked?</div></div>
      <div class="card layer" style="--c:#d97706"><div class="card-title">⚙️ Code</div><b>Thresholds & deterministic checks</b><div class="muted small" style="margin-top:4px">Approval tiers, budget, 365-day review currency on the policy date, registry/API conflicts, Security/Privacy/Legal triggers, injection scan.</div></div>
      <div class="card layer" style="--c:#059669"><div class="card-title">👤 Human</div><b>Sensitive approvals & exceptions</b><div class="muted small" style="margin-top:4px">Every approval, budget exception and override, recorded with a justification in the audit log.</div></div>
    </div>
    <div class="card" style="margin-top:14px"><div class="card-title">Workflow</div>
      <div class="flow"><div class="node">1 · Request<span>untrusted text</span></div><div class="arrow">→</div><div class="node">2 · Understand<span>LLM reads need</span></div><div class="arrow">→</div>
      <div class="node">3 · Gather evidence<span>8 tools · budget · catalog · vendor · policy</span></div><div class="arrow">→</div><div class="node">4 · Recommend<span>LLM draft → guardrails</span></div><div class="arrow">→</div>
      <div class="node" style="background:#fffbeb;border-color:#fde68a">5 · Human review<span>approvals & exceptions</span></div></div></div>
    <div class="grid2" style="margin-top:14px">
      <div class="card"><div class="card-title">A · Single agent</div><div class="flow"><div class="node" style="background:#f5f3ff">Procurement agent<span>8 tools incl. policy engine</span></div><div class="arrow">→</div><div class="node" style="background:#fef2f2">Guardrails<span>escalate, never de-escalate</span></div><div class="arrow">→</div><div class="node">Decision</div></div></div>
      <div class="card"><div class="card-title">B · Staged (2 agents)</div><div class="flow"><div class="node" style="background:#f5f3ff">Analyst<span>data tools</span></div><div class="arrow">→</div><div class="node" style="background:#fffbeb">Policy engine<span>code</span></div><div class="arrow">→</div><div class="node" style="background:#f5f3ff">Reviewer<span>decides</span></div><div class="arrow">→</div><div class="node" style="background:#fef2f2">Guardrails</div></div></div>
    </div>
    <div class="card" style="margin-top:14px"><div class="card-title">Guarantees</div><ul class="list">
      <li>The policy engine sets the floor for approvals and risk flags; the AI can add caution, never remove it.</li>
      <li>Every AI evidence claim must cite a tool called in the same run and match its numbers, otherwise it is dropped.</li>
      <li>Request text and vendor notes are untrusted data; embedded instructions are flagged and ignored.</li>
      <li>If a tool or the model is unavailable, the result is "unverified" and routed to a human, never assumed favourable.</li>
      <li>The copilot cannot purchase, approve spend, edit budgets or accept terms. Humans decide.</li></ul></div>`;
}

const VIEWS = { copilot: null, compare: renderCompare, eval: renderEval, audit: renderAudit, how: renderHow };
function showView(v) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("active", s.id === `view-${v}`));
  if (VIEWS[v]) Promise.resolve().then(VIEWS[v]).catch((e) => toast(e.message, true));
}

// ---------------------------------------------------------------- new request modal
async function openModal() {
  const emps = await api("/api/employees");
  $("#f-requester").innerHTML = emps.map((e) => `<option value="${esc(e.employee_id)}">${esc(e.employee_id)} · ${esc(e.name)} (${esc(e.department)})</option>`).join("");
  $("#modal").hidden = false;
}
async function submitNew(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const num = (k) => (fd.get(k) === "" ? null : Number(fd.get(k)));
  const body = {
    requester_id: fd.get("requester_id"), urgency: fd.get("urgency"), product_name: fd.get("product_name"), vendor_name: fd.get("vendor_name"),
    category: fd.get("category"), data_access_level: fd.get("data_access_level"), annual_cost_usd: num("annual_cost_usd"),
    user_count: num("user_count") === null ? null : Math.round(num("user_count")),
    requested_integrations: String(fd.get("requested_integrations") || "").split(",").map((s) => s.trim()).filter(Boolean),
    business_justification: fd.get("business_justification"),
  };
  try {
    const created = await api("/api/requests", { method: "POST", body: JSON.stringify(body) });
    state.requests = await api("/api/requests");
    $("#modal").hidden = true; ev.target.reset();
    select(created.request_id); runAnalysis();
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- wiring
document.addEventListener("click", (e) => {
  const t = e.target.closest("button, a");
  if (!t) return;
  if (t.matches(".req")) select(t.dataset.id);
  else if (t.matches("#seg button")) { state.arch = t.dataset.arch; renderCase(); renderSide(); }
  else if (t.id === "run") runAnalysis();
  else if (t.matches(".nav-item")) showView(t.dataset.view);
  else if (t.matches(".side-tab")) { state.sideTab = t.dataset.tab; renderSide(); }
  else if (t.matches(".act")) { state.action = t.dataset.action; document.querySelectorAll(".act").forEach((b) => b.classList.toggle("active", b === t)); }
  else if (t.id === "h-submit") recordDecision();
  else if (t.id === "btn-new") openModal();
  else if (t.id === "modal-close") $("#modal").hidden = true;
});
$("#search").addEventListener("input", renderInbox);
$("#new-form").addEventListener("submit", submitNew);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#modal").hidden = true; });

(async function init() {
  try {
    [state.meta, state.requests] = await Promise.all([api("/api/meta"), api("/api/requests")]);
    renderStatus();
    state.selected = state.requests[0]?.request_id;
    renderAll();
  } catch (e) { toast("Could not reach the copilot API: " + e.message, true); }
})();
